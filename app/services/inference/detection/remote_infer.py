"""RemoteInferProxy — 主进程侧推理子进程代理（req_id 异步管线 + 防泄漏 + 容错监督）。

把 GPU 前向拆进独立进程（见 infer_worker.py / 诊断文档）后，本类是主进程唯一对接口：
  · submit(batch)：给整批帧分配 req_id、把 cq 等**轻量元数据**留在 pending、只把帧送子进程；
  · _collect_loop：单线程抽子进程响应，据 req_id `pending.pop` 重组 FrameInference，走注入的
    write_back（= ModelWorkerService._write_back_results）落回主链路，并在主进程发 Prometheus；
  · _supervise_loop：看门狗，子进程死亡/CUDA wedge → 清孤儿 pending（计丢帧）+ 退避重 spawn。

**句柄不过进程边界**：cq 是进程内对象，留主进程按 req_id 关联即可（这正是本方案「架构改动最小」
的支点）。**防泄漏三重**：pending 有界（max_inflight，满则 submit 拒收计丢帧）、collect 每条
响应 pop 移除、子进程失败时清空 pending 并计 `infer_child_restart`。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from app.services.inference.models import DetectionTask, FrameInference
from app.services.inference.detection.infer_worker import run_infer_worker
from app.utils.metrics import frame_drop_total, infer_failure_total, infer_latency_ms

if TYPE_CHECKING:
    from app.services.client import ClientQueues
    from app.domain.detection import FrameDetections

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Pending:
    """在途批的每帧轻量记录：**不含 frame np.ndarray**（submit 后即弃引用，避免主/子双份帧内存）。

    cq 留主进程用于写回路由；frame_width/height 在 submit 时从原始帧盖章，供重组 FrameInference。
    """

    cq: "ClientQueues"   # 与 models.py 的运行句柄类型一致（写回路由，不过进程边界）
    task_id: int
    stage: str
    timestamp: float
    frame_width: int
    frame_height: int


class RemoteInferProxy:
    """推理子进程代理：req_id 异步提交 + 单收集线程写回 + 监督重启。"""

    def __init__(
        self,
        active_stages: List[str],
        write_back: Callable[[List[FrameInference]], None],
        *,
        max_inflight: int = 8,
        cuda_device: str = "0",
        ready_timeout: float = 120.0,
        response_timeout: float = 15.0,
        drain_timeout: float = 5.0,
        restart_backoff_max: float = 30.0,
        max_restarts: Optional[int] = None,
        supervise_interval: float = 1.0,
    ):
        """
        Args:
            active_stages: 需在子进程建 pool 的 stage 主键（= 主进程已筛出有 detector 的 stage）。
            write_back: 写回回调，收 List[FrameInference]（注入 ModelWorkerService._write_back_results）。
            max_inflight: 在途批数上限（背压 + 防 pending 无界；满则 submit 返回 False）。
            cuda_device: 子进程 CUDA_VISIBLE_DEVICES（""=CPU，仅测试）。
            ready_timeout: 等子进程 warmup 就绪的超时（模型加载慢，给足）。
            response_timeout: 有在途批但超此秒数无任何响应 → 判 CUDA wedge，杀+重启。
            drain_timeout: stop() 排空在途批的上限。
            restart_backoff_max: 重启退避上限秒。
            max_restarts: 累计重启上限（None=不限）。
        """
        self._active_stages = list(active_stages)
        self._write_back = write_back
        self._max_inflight = max_inflight
        self._cuda_device = cuda_device
        self._ready_timeout = ready_timeout
        self._response_timeout = response_timeout
        self._drain_timeout = drain_timeout
        self._restart_backoff_max = restart_backoff_max
        self._max_restarts = max_restarts
        self._supervise_interval = supervise_interval

        # spawn 上下文：CUDA + fork 不安全，且我们要主进程保持 CUDA-free。
        import multiprocessing as _mp
        self._ctx = _mp.get_context("spawn")

        self._req_q = None
        self._resp_q = None
        self._ready_ev = None
        self._proc = None
        # 子进程生命周期锁：串行化「建+起子进程」与「杀子进程」，关停竞态下不留孤儿
        # （spawn 与 kill 互斥；停机标志置位后 spawn 一律早退，不新起进程）。
        self._proc_lock = threading.Lock()

        self._pending: Dict[int, List[_Pending]] = {}
        self._lock = threading.Lock()
        self._inflight = 0
        self._next_req_id = 0
        self._last_resp_ts = 0.0
        self._restarts = 0

        self._child_ready = threading.Event()   # 子进程就绪（可接收 submit）
        self._stop_event = threading.Event()     # 全局停机（两线程退出）
        self._no_restart = threading.Event()     # 监督线程停止重启（stop 期间）

        self._collector: Optional[threading.Thread] = None
        self._supervisor: Optional[threading.Thread] = None

    # ────────────────────────── 生命周期 ──────────────────────────

    def start(self) -> None:
        """spawn 子进程并等就绪，起 collector + supervisor 线程。"""
        self._spawn_child()
        self._collector = threading.Thread(target=self._collect_loop, name="InferCollector", daemon=True)
        self._supervisor = threading.Thread(target=self._supervise_loop, name="InferSupervisor", daemon=True)
        self._collector.start()
        self._supervisor.start()
        logger.info("[RemoteInferProxy] started (stages=%s, max_inflight=%d)", self._active_stages, self._max_inflight)

    def _spawn_child(self) -> None:
        """建队列 + 起子进程，阻塞等就绪屏障。失败/停机时不置 ready。

        建+起在 `_proc_lock` 内、与 `_kill_child` 互斥：一旦停机标志已置位就早退不新起，
        杜绝「停机杀了旧进程后监督线程又起一个孤儿」的竞态（review #1）。就绪等待放锁外，
        避免 120s ready_timeout 期间阻塞 kill。
        """
        with self._proc_lock:
            if self._stop_event.is_set() or self._no_restart.is_set():
                return  # 停机中：不再新起子进程
            self._child_ready.clear()
            self._req_q = self._ctx.Queue(maxsize=self._max_inflight * 4)
            self._resp_q = self._ctx.Queue(maxsize=self._max_inflight * 4)
            self._ready_ev = self._ctx.Event()
            self._proc = self._ctx.Process(
                target=run_infer_worker,
                args=(self._req_q, self._resp_q, self._ready_ev, self._active_stages, self._cuda_device),
                name="InferChild",
                daemon=True,
            )
            self._proc.start()
            ready_ev, pid = self._ready_ev, self._proc.pid
        logger.info("[RemoteInferProxy] 子进程 spawn pid=%s，等就绪…", pid)
        ready = ready_ev.wait(timeout=self._ready_timeout)
        self._last_resp_ts = time.monotonic()
        if not ready:
            logger.error("[RemoteInferProxy] 子进程 %ss 未就绪（可能模型加载慢/失败）", self._ready_timeout)
            # 不置 child_ready：submit 会拒收；监督线程后续按死活/wedge 判定处理。
            return
        self._child_ready.set()
        logger.info("[RemoteInferProxy] 子进程就绪 pid=%s", self._proc.pid)

    def stop(self) -> None:
        """排空在途批（写回落盘）→ 停两线程 → 杀子进程 → 剩余在途计丢帧。"""
        self._no_restart.set()  # 监督线程不再重启

        # 1. 排空在途：子进程仍活着，让 collector 把在途批写回（先于上层 feature_store.flush）
        deadline = time.monotonic() + self._drain_timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._inflight == 0:
                    break
            time.sleep(0.02)

        # 2. 停两线程
        self._stop_event.set()
        for t in (self._collector, self._supervisor):
            if t is not None:
                t.join(timeout=2.0)

        # 3. 杀子进程
        self._kill_child()

        # 4. 剩余在途 → 计丢帧（未及排空；丢帧非损坏，与既有 backpressure 同性质）
        with self._lock:
            leftover = sum(len(r) for r in self._pending.values())
            self._pending.clear()
            self._inflight = 0
        if leftover:
            frame_drop_total.labels(reason="infer_child_down").inc(leftover)
        logger.info("[RemoteInferProxy] stopped (leftover_dropped=%d)", leftover)

    def _kill_child(self) -> None:
        """terminate→join→kill→join 收尸，并关闭队列（镜像 decoder.py 的硬收尸）。

        在 `_proc_lock` 内、与 `_spawn_child` 互斥：停机路径先置 `_stop_event`/`_no_restart`
        再调本方法，与之竞争的 spawn 要么已完成（此处杀掉其起的进程）、要么被早退挡住。
        """
        with self._proc_lock:
            proc = self._proc
            if proc is not None:
                try:
                    if proc.is_alive():
                        proc.terminate()
                        proc.join(timeout=2.0)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=2.0)
                except Exception as e:  # pragma: no cover
                    logger.warning("[RemoteInferProxy] kill child failed: %s", e)
            for q in (self._req_q, self._resp_q):
                try:
                    if q is not None:
                        q.close()
                except Exception:  # pragma: no cover
                    pass
            self._child_ready.clear()

    # ────────────────────────── 提交 ──────────────────────────

    def submit(self, batch: List[DetectionTask]) -> bool:
        """提交一批（同一 stage）。返回 False 表示未提交（在途满/子进程未就绪/停机），调用方计丢帧。

        cq 等留 pending，只把帧送子进程；submit 后不再持有 frame 引用（帧仅活在子进程 + 在途队列）。
        """
        if not batch:
            return True
        if self._stop_event.is_set() or not self._child_ready.is_set():
            return False

        stage = batch[0].stage
        with self._lock:
            if self._inflight >= self._max_inflight:
                return False
            req_id = self._next_req_id
            self._next_req_id += 1
            self._pending[req_id] = [
                _Pending(
                    cq=req.cq, task_id=req.task_id, stage=req.stage, timestamp=req.timestamp,
                    frame_width=int(req.frame.shape[1]), frame_height=int(req.frame.shape[0]),
                )
                for req in batch
            ]
            self._inflight += 1

        frames = [req.frame for req in batch]
        timestamps = [req.timestamp for req in batch]
        try:
            self._req_q.put((req_id, stage, frames, timestamps), timeout=1.0)
        except (queue.Full, ValueError, OSError) as e:
            # 送不进去（队列满/已关闭）：回滚 pending，计丢帧
            with self._lock:
                if self._pending.pop(req_id, None) is not None:
                    self._inflight -= 1
            logger.warning("[RemoteInferProxy] req_q put 失败，丢弃 req_id=%s: %s", req_id, e)
            return False
        return True

    # ────────────────────────── 收集 ──────────────────────────

    def _collect_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._resp_q.get(timeout=0.5)
            except queue.Empty:
                continue
            except (OSError, EOFError, ValueError):
                # 队列在重启窗口被替换/关闭：短歇后下轮读新队列
                time.sleep(0.05)
                continue
            self._last_resp_ts = time.monotonic()
            try:
                self._handle_response(item)
            except Exception as e:  # pragma: no cover
                logger.error("[RemoteInferProxy] handle_response 异常: %s", e, exc_info=True)

    def _handle_response(self, item) -> None:
        req_id, merged = item
        with self._lock:
            records = self._pending.pop(req_id, None)   # ← 防泄漏核心：pop 移除在途条目
            if records is not None:
                self._inflight -= 1
        if records is None:
            # 孤儿响应（子进程重启后的旧响应/重复）——忽略，不写回
            return

        frame_infs: List[FrameInference] = []
        for i, rec in enumerate(records):
            per_frame = merged[i] if i < len(merged) else {}
            frame_infs.append(FrameInference(
                task_id=rec.task_id, stage=rec.stage, timestamp=rec.timestamp,
                detections=per_frame, cq=rec.cq,
                frame_width=rec.frame_width, frame_height=rec.frame_height,
            ))
        # 写回主链路：其内 cq.is_active() 门 + feature_store owner fence 处理迟到/跨 run
        self._write_back(frame_infs)
        self._emit_stats(merged, len(records))

    @staticmethod
    def _emit_stats(merged: List[Dict[str, "FrameDetections"]], n: int) -> None:
        """在主进程发 Prometheus（子进程 registry 无效，故埋点上移）。

        观测量直接取自 merged 里的 `FrameDetections`（不另立 stats 通道）：成功读 metadata
        ["infer_ms"] 发延迟、失败（success=False）读 metadata["error_type"] 计失败。每模型每批
        只发一次（seen 去重）——merged 是逐帧展开，同模型 N 帧共享同一批观测。
        """
        if n <= 0:
            return
        seen = set()
        for per_frame in merged:
            for name, fd in per_frame.items():
                if name in seen:
                    continue
                seen.add(name)
                try:
                    if fd.success:
                        infer_ms = fd.metadata.get("infer_ms")
                        if infer_ms is not None:
                            infer_latency_ms.labels(model=name).observe(infer_ms / n)
                    else:
                        infer_failure_total.labels(
                            model=name, error_type=fd.metadata.get("error_type", "Unknown"),
                        ).inc()
                except Exception:  # pragma: no cover
                    pass

    # ────────────────────────── 监督 ──────────────────────────

    def _supervise_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._supervise_interval)
            if self._stop_event.is_set() or self._no_restart.is_set():
                continue
            proc = self._proc
            dead = proc is None or not proc.is_alive()
            with self._lock:
                inflight = self._inflight
            wedged = inflight > 0 and (time.monotonic() - self._last_resp_ts) > self._response_timeout
            if dead or wedged:
                self._handle_child_failure(dead=dead, wedged=wedged)

    def _handle_child_failure(self, *, dead: bool, wedged: bool) -> None:
        """子进程死亡/wedge：停 submit、清孤儿 pending（计丢帧）、退避重 spawn。"""
        self._child_ready.clear()   # 立即停止接收 submit
        logger.error("[RemoteInferProxy] 子进程失败 dead=%s wedged=%s，清理在途并重启", dead, wedged)
        self._kill_child()

        with self._lock:
            orphaned = sum(len(r) for r in self._pending.values())
            self._pending.clear()
            self._inflight = 0
        if orphaned:
            frame_drop_total.labels(reason="infer_child_restart").inc(orphaned)
            logger.warning("[RemoteInferProxy] 丢弃在途 %d 帧（子进程重启）", orphaned)

        if self._stop_event.is_set() or self._no_restart.is_set():
            return
        self._restarts += 1
        if self._max_restarts is not None and self._restarts > self._max_restarts:
            logger.critical("[RemoteInferProxy] 超过最大重启次数 %d，放弃推理子进程", self._max_restarts)
            return
        delay = min(2 ** self._restarts, self._restart_backoff_max)
        logger.info("[RemoteInferProxy] %.1fs 后重启子进程（第 %d 次）", delay, self._restarts)
        if self._stop_event.wait(delay):
            return
        try:
            self._spawn_child()
        except Exception as e:  # pragma: no cover
            logger.error("[RemoteInferProxy] 重 spawn 失败: %s", e, exc_info=True)
