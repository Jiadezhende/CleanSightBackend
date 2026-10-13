"""RemoteRenderProxy — 渲染子进程池代理（共享内存槽 + 每键单飞 + 监督重启）。

    proxy = RemoteRenderProxy(processes=2, slot_bytes=640 * 480 * 3, slots=32)
    proxy.start()
    proxy.in_flight(key)                                        # 该键是否有在途渲染
    proxy.submit(key, frame, specs, stage, events, ctx) -> bool # False：未就绪 / 槽满 / 帧放不进 / 键在途
    for ctx, annotated, render_s, ok in proxy.drain(): ...      # 调用方线程非阻塞拉完成结果
    proxy.stop()

**每键单飞**：同一 key（task_id）同时最多一个在途渲染——processed 轨要求帧按 ts 序入队，
多进程并发渲染同一 run 会乱序。**槽位生命周期归主进程**：drain 读出结果后归还；子进程死亡 /
超时未答 / 久不就绪时 kill 全部子进程、清在途（计丢帧）、reset 环、重建队列后退避重起。
依据见 docs/update/20261013_VIZ_RENDER_PROCESSES.md。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, Hashable, List, NamedTuple, Optional, Tuple

import numpy as np

from app.services.utils.frame_ring import SharedFrameRing
from app.services.utils.metrics import frame_drop_total
from .render_child import run_renderer

logger = logging.getLogger(__name__)


class _Req(NamedTuple):
    key: Hashable
    slot: int
    ctx: Any
    submitted_at: float


class RemoteRenderProxy:
    """渲染子进程池代理：调用方线程 submit / drain，监督线程负责失败重启。"""

    def __init__(
        self,
        processes: int,
        slot_bytes: int,
        slots: int,
        *,
        ready_timeout: float = 60.0,
        response_timeout: float = 2.0,
        supervise_interval: float = 0.5,
        restart_backoff_max: float = 30.0,
    ):
        """
        Args:
            processes: 渲染子进程数。
            slot_bytes: 单槽字节（= 单帧 W×H×3）；放不进的帧 submit 返回 False，由调用方就地渲染。
            slots: 槽位数，应 ≥ 并发 run 数（每 run 至多一个在途）。
            ready_timeout: 子进程就绪（import cv2 / PIL）超时，超过按失败重启。
            response_timeout: 最老在途请求超过此秒数未答复 → 判 wedge，全部重启。
        """
        self._processes = processes
        self._slot_bytes = slot_bytes
        self._slots = slots
        self._ready_timeout = ready_timeout
        self._response_timeout = response_timeout
        self._supervise_interval = supervise_interval
        self._restart_backoff_max = restart_backoff_max

        import multiprocessing as _mp
        self._ctx = _mp.get_context("spawn")

        self._ring: Optional[SharedFrameRing] = None
        self._req_q = None
        self._resp_q = None
        self._procs: List[Any] = []
        self._ready_evs: List[Any] = []
        self._spawn_at = 0.0
        self._proc_lock = threading.Lock()   # 串行化「建+起子进程」与「杀子进程」

        self._lock = threading.Lock()        # 护 _pending / _by_key 与槽位读写归还
        self._pending: Dict[int, _Req] = {}
        self._by_key: Dict[Hashable, int] = {}
        self._next_req_id = 0
        self._restarts = 0

        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._supervisor: Optional[threading.Thread] = None

    # ────────────────────────── 生命周期 ──────────────────────────

    def start(self) -> None:
        self._ring = SharedFrameRing(self._slots, self._slot_bytes, prefix="csviz")
        self._spawn_children()
        self._supervisor = threading.Thread(
            target=self._supervise_loop, name="RenderSupervisor", daemon=True,
        )
        self._supervisor.start()
        logger.info(
            "[RemoteRenderProxy] started (processes=%d, ring=%dx%dB, ready=%s)",
            self._processes, self._slots, self._slot_bytes, self._ready.is_set(),
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._ready.clear()
        if self._supervisor is not None:
            self._supervisor.join(timeout=2.0)
        self._kill_children()
        dropped = self._clear_inflight()
        if dropped:
            frame_drop_total.labels(reason="viz_child_down").inc(dropped)
        if self._ring is not None:
            self._ring.close()
        logger.info("[RemoteRenderProxy] stopped (inflight_dropped=%d)", dropped)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def _spawn_children(self) -> None:
        with self._proc_lock:
            if self._stop_event.is_set():
                return
            self._req_q = self._ctx.Queue(maxsize=self._slots)
            self._resp_q = self._ctx.Queue(maxsize=self._slots)
            # 收尸时主动断 req_q 读端逼停 feeder（见 _kill_children），EPIPE 是预期路径；须在首次 put 前置位
            self._req_q._ignore_epipe = True
            evs = [self._ctx.Event() for _ in range(self._processes)]
            procs = [
                self._ctx.Process(
                    target=run_renderer,
                    args=(self._req_q, self._resp_q, ev, self._ring.spec),
                    name=f"RenderChild-{i}",
                    daemon=True,
                )
                for i, ev in enumerate(evs)
            ]
            for p in procs:
                p.start()
            self._procs, self._ready_evs = procs, evs
            self._spawn_at = time.monotonic()
        deadline = time.monotonic() + self._ready_timeout
        for ev in evs:
            ev.wait(timeout=max(0.0, deadline - time.monotonic()))
        if all(ev.is_set() for ev in evs):
            self._ready.set()
            logger.info("[RemoteRenderProxy] 子进程就绪 pids=%s", [p.pid for p in procs])
        else:
            logger.error(
                "[RemoteRenderProxy] 子进程 %.0fs 未全部就绪，交给监督线程判失败重启", self._ready_timeout,
            )

    def _kill_children(self) -> None:
        """terminate→join→kill→join；队列断读端 + 撤 join + 关（理由同 RemoteInferProxy._kill_child）。"""
        with self._proc_lock:
            for p in self._procs:
                try:
                    if p.is_alive():
                        p.terminate()
                        p.join(timeout=2.0)
                    if p.is_alive():
                        p.kill()
                        p.join(timeout=2.0)
                except Exception as e:  # pragma: no cover
                    logger.warning("[RemoteRenderProxy] kill child failed: %s", e)
            if self._req_q is not None:
                try:
                    self._req_q._reader.close()
                except Exception:  # pragma: no cover
                    pass
            for q in (self._req_q, self._resp_q):
                try:
                    if q is not None:
                        q.cancel_join_thread()
                        q.close()
                except Exception:  # pragma: no cover
                    pass
            self._procs = []

    def _clear_inflight(self) -> int:
        with self._lock:
            n = len(self._pending)
            self._pending.clear()
            self._by_key.clear()
            if self._ring is not None:
                self._ring.reset()
        return n

    # ────────────────────────── 提交 / 收取 ──────────────────────────

    def in_flight(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._by_key

    def submit(self, key, frame: np.ndarray, specs, stage: str, events, ctx) -> bool:
        ring = self._ring
        if not self._ready.is_set() or ring is None or not ring.fits(frame):
            return False
        with self._lock:
            if key in self._by_key:
                return False
            slots = ring.acquire(1)
            if slots is None:
                return False
            req_id = self._next_req_id
            self._next_req_id += 1
            self._pending[req_id] = _Req(key, slots[0], ctx, time.monotonic())
            self._by_key[key] = req_id
        try:
            ring.write(slots[0], frame)
            self._req_q.put((req_id, slots[0], frame.shape, specs, stage, list(events or [])), timeout=0.05)
        except (queue.Full, ValueError, OSError) as e:
            self._forget(req_id)
            logger.debug("[RemoteRenderProxy] req_q put 失败 req_id=%s: %s", req_id, e)
            return False
        return True

    def _forget(self, req_id: int) -> None:
        with self._lock:
            req = self._pending.pop(req_id, None)
            if req is not None:
                self._by_key.pop(req.key, None)
                self._ring.release([req.slot])

    def drain(self) -> List[Tuple[Any, np.ndarray, float, bool]]:
        """非阻塞取走全部已完成渲染：[(ctx, annotated, render_seconds, ok)]。孤儿答复（重启前的旧请求）丢弃。"""
        out: List[Tuple[Any, np.ndarray, float, bool]] = []
        q = self._resp_q
        if q is None:
            return out
        while True:
            try:
                req_id, shape, render_s, ok = q.get_nowait()
            except queue.Empty:
                break
            except (OSError, EOFError, ValueError):
                break   # 队列在重启窗口被关闭/替换：下轮读新队列
            with self._lock:
                req = self._pending.pop(req_id, None)
                if req is None:
                    continue
                self._by_key.pop(req.key, None)
                annotated = self._ring.read(req.slot, shape)
                self._ring.release([req.slot])
            out.append((req.ctx, annotated, render_s, ok))
        return out

    # ────────────────────────── 监督 ──────────────────────────

    def _supervise_loop(self) -> None:
        while not self._stop_event.wait(self._supervise_interval):
            procs = self._procs
            if not procs:
                continue
            dead = any(not p.is_alive() for p in procs)
            with self._lock:
                oldest = min((r.submitted_at for r in self._pending.values()), default=None)
            wedged = oldest is not None and time.monotonic() - oldest > self._response_timeout
            not_ready = False
            if not self._ready.is_set() and not dead:
                if all(ev.is_set() for ev in self._ready_evs):
                    self._ready.set()
                    logger.warning("[RemoteRenderProxy] 子进程迟到就绪，恢复接收")
                else:
                    not_ready = time.monotonic() - self._spawn_at > self._ready_timeout
            if dead or wedged or not_ready:
                self._handle_failure(dead=dead, wedged=wedged, not_ready=not_ready)

    def _handle_failure(self, *, dead: bool, wedged: bool, not_ready: bool) -> None:
        self._ready.clear()
        logger.error(
            "[RemoteRenderProxy] 子进程失败 dead=%s wedged=%s not_ready=%s，清理在途并重启",
            dead, wedged, not_ready,
        )
        self._kill_children()
        dropped = self._clear_inflight()
        if dropped:
            frame_drop_total.labels(reason="viz_child_down").inc(dropped)
        if self._stop_event.is_set():
            return
        self._restarts += 1
        delay = min(2 ** self._restarts, self._restart_backoff_max)
        logger.info("[RemoteRenderProxy] %.1fs 后重启子进程（第 %d 次）", delay, self._restarts)
        if self._stop_event.wait(delay):
            return
        try:
            self._spawn_children()
        except Exception as e:  # pragma: no cover
            logger.error("[RemoteRenderProxy] 重 spawn 失败: %s", e, exc_info=True)
