"""Stage 感知的帧调度器（取帧 + 组批 + 直接提交推理子进程，单提交者）。

职责：
- 轮询所有客户端的 ca_ready 队列，按 stage 分组入 deque（保证流间公平 Round-Robin）
- 同一循环内 peek-commit 轮转排空：每 stage 每圈 peek 一批 submit，proxy 接了才 popleft、
  被拒即停（帧留 deque，背压沿链上传）。单提交者，不感知 proxy 的 inflight/cap。
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict, List, Optional

from app.services.client import ClientManager, client_manager
from app.services.inference.types import DetectionTask
from app.utils.metrics import frame_drop_total
from app.utils.pressure import (
    DEFAULT_HIGH_WATERMARK_RATIO,
    REASON_QUEUE_HIGH_WATERMARK,
    PressureReporter,
)
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class StageAwareDispatcher:
    """Stage感知的帧调度器（直接引用 ClientManager）。

    职责：
    - 轮询所有客户端的 ca_ready 队列，按 stage 分组入 deque（流间公平 Round-Robin）
    - 同一循环内 peek-commit 轮转排空组批、直接 submit 到推理子进程（单提交者）

    改进点：
    - 直接引用全局 ClientManager，实时获取客户端列表
    - 无需手动刷新，自动同步客户端变化
    - 新客户端加入或离开无延迟
    """

    def __init__(
        self,
        max_batch_per_stage: int = 8,
        fetch_interval: float = 0.01,  # 10ms 轮询间隔
        client_manager_instance: Optional["ClientManager"] = None,
        *,
        active_stages: Optional[List[str]] = None,
        stage_batch_sizes: Optional[Dict[str, int]] = None,
        submit_batch: Optional[Callable[[List[DetectionTask]], bool]] = None,
    ):
        """
        Args:
            max_batch_per_stage: 每个 stage 最大 batch 大小
            fetch_interval: 轮询间隔（秒）
            client_manager_instance: ClientManager 实例（可选，用于依赖注入测试）
            active_stages: 需提交的 stage 主键（有 detector 的 stage）；缺省则只取帧不提交。
            stage_batch_sizes: 各 stage 组批上限；未列出的 stage 用 max_batch_per_stage。
            submit_batch: 提交回调（= RemoteInferProxy.submit，返回是否接收）。本类是唯一提交者，
                peek-commit 轮转：接了才 popleft、被拒即停（帧留 deque），不预读 proxy 的在途额度。
                缺省（无 submit_batch）则只取帧不提交。
        """
        self._client_manager = client_manager_instance or client_manager
        self.max_batch_per_stage = max_batch_per_stage
        self.fetch_interval = fetch_interval

        # 提交侧注入（消费半）：取帧后在同一循环里组批直提，替代原 per-stage 提交线程。
        self._active_stages: List[str] = list(active_stages or [])
        self._stage_batch_sizes: Dict[str, int] = dict(stage_batch_sizes or {})
        self._submit_batch = submit_batch

        # 背压反馈通道（drain 侧写、admit 侧读的单向异步口子）：预留给未来「入口降帧」——
        # drain 撞 proxy 满时可在此沉淀各 stage 压力，下一轮 _fetch 经 _admit_to_stage 读它抽稀。
        # 本次不接通（drain 不写、admit 恒 True），仅固化数据流向，接通时无需再改结构。
        self._stage_backpressure: Dict[str, Any] = {}

        self._stop_event = threading.Event()
        self._dispatch_thread: Optional[threading.Thread] = None

        # Stage分组队列：{stage: deque[DetectionTask]}
        self._stage_queues: Dict[str, Deque[DetectionTask]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self._lock = threading.Lock()

        # 统计信息
        self._stats = {
            "total_dispatched": 0,
            "by_stage": defaultdict(int),
        }

        # 各 stage 因 maxlen 满而静默淘汰最旧帧的累计计数（推理掉速时的真实积压信号）
        self._stage_drops: Dict[str, int] = defaultdict(int)

        # 压力日志（[PRESSURE] resource=stage_queue）：每 ~1s 采样，限频与 delta 记账在
        # PressureReporter 里（每 stage 每 10s 至多一条，平稳时静默）。
        # **只报自己的 stage deque**：ca_processed 归 ClientQueues 自己报（每资源单一所有者）。
        self._round_counter: int = 0
        self._check_every_rounds: int = max(1, int(1.0 / self.fetch_interval))
        self._pressure_queue_ratio: float = DEFAULT_HIGH_WATERMARK_RATIO
        self._stage_pressure: Dict[str, PressureReporter] = {}
        # 各 stage 被 proxy 拒收（submit 返 False）的累计次数。proxy 内部 inflight 满是它的
        # 私有状态、不外泄，但**拒收这件事本身**在提交侧看得见：这里计数并并入压力行，
        # 让「下游满了发不出去」不再是一个静默的布尔值。
        self._stage_rejects: Dict[str, int] = defaultdict(int)

    def start(self):
        """启动调度线程"""
        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return

        self._stop_event.clear()
        self._dispatch_thread = threading.Thread(
            target=guarded_run,
            args=(self._dispatch_loop, self._stop_event, "StageAwareDispatcher"),
            daemon=True,
        )
        self._dispatch_thread.start()
        logger.debug(
            "[StageAwareDispatcher] Started | interval=%.1fms", self.fetch_interval*1000
        )

    def stop(self):
        """停止调度线程"""
        self._stop_event.set()
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=2.0)
        logger.debug("[StageAwareDispatcher] Stopped")

    def _dispatch_loop(self):
        """调度循环：取帧入 deque → 据额度组批直提子进程。"""
        while not self._stop_event.is_set():
            try:
                self._fetch_and_dispatch_round()   # 生产：ca_ready → _stage_queues
                self._drain_and_submit()           # 消费：_stage_queues → proxy.submit
            except Exception as e:
                logger.error("[StageAwareDispatcher] Dispatch error: %s", e, exc_info=True)

            self._round_counter += 1
            if self._round_counter % self._check_every_rounds == 0:
                self._log_pressure_snapshot()

            # 使用 Event.wait 可及时响应 stop 信号
            self._stop_event.wait(self.fetch_interval)

    def _fetch_and_dispatch_round(self):
        """一轮调度：轮询所有客户端，取帧并按 stage 分组。

        容错设计：
        - 动态从 ClientManager 获取最新客户端列表（实时同步）
        - 客户端动态添加/移除自动生效，无延迟
        - 队列为空时跳过，不影响其他客户端
        """
        # 动态获取客户端列表（实时同步，无需刷新）
        # ClientManager.snapshot() 返回字典副本，迭代安全
        clients = self._client_manager.snapshot()
        for task_id, cq in clients.items():
            # 从 ca_ready 队列取一帧（FIFO，保证公平）
            # 使用封装方法，避免直接访问内部队列
            frame_data = cq.pop_ca_ready()
            if frame_data is None:
                # 队列为空或并发场景下被其他线程取走
                continue

            stage = cq.stage  # 不可变身份，直读

            # 背压反馈接缝（本次透明恒放行）：未来「入口降帧」在此据 _stage_backpressure[stage]
            # 按 ts 相位抽稀，把 drain 侧感知的下游压力回传到取帧侧。
            if not self._admit_to_stage(stage):
                continue

            # 构造推理请求：捕获该 CQ 句柄随请求同行，写回凭它投递、不反查
            # （cq 即当前 snapshot 迭代出的对象，与 pop_ca_ready() 同源）。
            req = DetectionTask(
                task_id=task_id,
                stage=stage,
                timestamp=frame_data.timestamp,
                frame=frame_data.frame,
                cq=cq,
            )

            # 按 stage 分组入队
            dropped = False
            with self._lock:
                q = self._stage_queues[stage]
                # 队列已满 → append 会静默淘汰最旧帧，先计数（对齐 ca_raw 的 frames_dropped_raw）
                if q.maxlen is not None and len(q) >= q.maxlen:
                    self._stage_drops[stage] += 1
                    dropped = True
                q.append(req)
                self._stats["total_dispatched"] += 1
                self._stats["by_stage"][stage] += 1

            # Prometheus 计数放锁外（Counter 自身线程安全，避免占用调度锁）
            if dropped:
                frame_drop_total.labels(reason="infer_backlog").inc()

    def _drain_and_submit(self) -> None:
        """peek-commit 轮转排空：从各 active stage deque 组批并直接 submit（单提交者）。

        本方法是唯一提交者。**不预读 proxy 在途额度**——限流是 proxy 固有职责，`submit` 返 False
        即背压信号。每圈按轮换 offset 遍历各 stage：peek 一批（切片看、不移除）→ submit，接了才
        `_commit_pop`（popleft），被拒即 return（帧原封留 deque，背压沿链上传、不丢帧）。外层 while
        循环直到某一整圈无任何进展（全空/全被拒）为止；每 stage 每圈只发一批，天然公平不饿死。
        """
        if self._submit_batch is None or not self._active_stages:
            return

        n = len(self._active_stages)
        offset = self._round_counter % n
        while True:
            progressed = False
            for k in range(n):
                stage = self._active_stages[(offset + k) % n]
                bsz = self._stage_batch_sizes.get(stage, self.max_batch_per_stage)
                batch = self._peek_batch(stage, bsz)   # 切片看，不移除
                if not batch:
                    continue
                if self._submit_batch(batch):
                    self._commit_pop(stage, len(batch))  # 接了才 popleft
                    progressed = True
                else:
                    # proxy 限流/未就绪：停本轮，帧留 deque，不丢帧。只记数不打日志——
                    # 满的时候这里每 10ms 撞一次，压力行由 1s 采样按周期统一打。
                    self._stage_rejects[stage] += 1
                    return
            if not progressed:
                return  # 整圈无进展：全空

    def _peek_batch(self, stage: str, max_size: int) -> List[DetectionTask]:
        """非阻塞查看指定 stage deque 的前 ≤max_size 帧（切片，不移除；线程安全）。"""
        with self._lock:
            q = self._stage_queues.get(stage)
            if not q:
                return []
            return list(itertools.islice(q, 0, min(max_size, len(q))))

    def _commit_pop(self, stage: str, n: int) -> None:
        """提交成功后从 deque 左端移除已提交的 n 帧（线程安全）。

        单提交者单线程顺序 peek→submit→commit，其间无并发写 deque，popleft 的正是 peek 所见。
        """
        with self._lock:
            q = self._stage_queues.get(stage)
            if not q:
                return
            for _ in range(min(n, len(q))):
                q.popleft()

    def _admit_to_stage(self, stage: str) -> bool:
        """取帧准入钩子（背压反馈接缝）。**本次恒返回 True**（零行为变化）。

        这是「入口降帧」的唯一挂载点：未来据 self._stage_backpressure[stage]（drain 侧沉淀的
        下游压力）+ ts 相位在此抽稀，把 proxy 限流的背压回传到取帧侧、替代 deque 满被动淘汰。
        """
        return True

    def get_stage_queue_depths(self) -> Dict[str, int]:
        """获取各 stage 队列深度（调试用）"""
        with self._lock:
            return {stage: len(queue) for stage, queue in self._stage_queues.items()}

    def get_stage_drops(self) -> Dict[str, int]:
        """获取各 stage 因 maxlen 满而静默淘汰的累计丢帧数。"""
        with self._lock:
            return dict(self._stage_drops)

    def _log_pressure_snapshot(self) -> None:
        """采一次各 stage deque 的压力快照，交给 per-stage PressureReporter 按周期打。

        本方法只做两件事：锁内取标量快照、锁外喂 reporter。限频与 drop/reject delta 记账
        都在 PressureReporter 里（见 app/utils/pressure.py）。

        **只报自己拥有的资源**：stage deque 是本类独有的积压点（proxy 拒收时帧留在这里、
        满了静默淘汰最旧帧）。ca_processed 由 ClientQueues 在其 append 内自报，本类不代劳。
        `reject_total` 例外——那是本类调 submit 时看到的返回值，是提交侧的自有观测。

        日志失败绝不影响调度热路径——整体 try/except 包裹。
        """
        try:
            # 锁内只取标量（队头 ts 仅在越水位时多读一次，平稳期不付代价）
            with self._lock:
                snapshot = []
                for stage, q in self._stage_queues.items():
                    depth = len(q)
                    cap = q.maxlen or 0
                    oldest_ts = (
                        q[0].timestamp
                        if depth and cap and depth >= cap * self._pressure_queue_ratio
                        else None
                    )
                    snapshot.append((
                        stage, depth, cap, oldest_ts,
                        self._stage_drops.get(stage, 0), self._stage_rejects.get(stage, 0),
                    ))

            now = time.time()
            for stage, depth, cap, oldest_ts, drops, rejects in snapshot:
                reporter = self._stage_pressure.get(stage)
                if reporter is None:
                    reporter = PressureReporter(
                        "dispatcher", "stage_queue", identity={"stage": stage},
                    )
                    self._stage_pressure[stage] = reporter
                # 谓词只写水位；「丢帧/拒收仍在涨」由 reporter 并入判定
                # （见 pressure.py：压力 = 谓词 OR 任一 *_total 增长）
                reporter.observe(
                    bool(cap) and depth >= cap * self._pressure_queue_ratio,
                    reason=REASON_QUEUE_HIGH_WATERMARK,
                    depth=depth,
                    capacity=cap,
                    utilization=(depth / cap) if cap else None,
                    oldest_age_ms=((now - oldest_ts) * 1000.0) if oldest_ts else None,
                    drop_total=drops,
                    reject_total=rejects,
                )
        except Exception as e:
            logger.debug("[StageAwareDispatcher] pressure snapshot failed: %s", e)
