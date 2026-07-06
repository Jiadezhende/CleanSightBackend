"""HLS 分段拉取 Worker（PULL 模型）

职责：
- 后台 daemon 线程，每 interval_seconds 扫一遍所有活跃 ClientQueues
- 对每个 CQ 把已攒满的整段（ca_segment_len 帧）拉走，入 HLS 持久化队列
- 取代旧的 PUSH（ClientQueues.append_ca_* 内直接调 persist_hls_segment）——
  分段判定 + 落盘触发归 persistence，ClientQueues 退回纯缓冲容器

依赖注入 snapshot_fn / persist_fn，不在本模块 import 单例，便于 seam 单测。
运行期只落整段；末尾不足一段的残帧由 RunController 拆除时的
PersistenceManager.flush_residual_segments 收尾（语义同旧 PUSH）。
"""

import logging
import threading
from typing import Callable, List, Mapping

from app.domain.frame import Frame

logger = logging.getLogger(__name__)


class HLSSegmentSweeper:
    """周期性从活跃 CQ 拉取攒满的 HLS 整段。"""

    def __init__(
        self,
        snapshot_fn: Callable[[], Mapping[int, object]],
        persist_fn: Callable[..., bool],
        interval_seconds: float = 1.0,
    ):
        """
        Args:
            snapshot_fn: 返回 {task_id: ClientQueues} 只读快照（= client_manager.snapshot）。
            persist_fn: 落盘入队回调，签名 (*, task_id, step_id, segment_type, frames) -> bool
                        （= PersistenceManager.persist_hls_segment）。
            interval_seconds: 扫描间隔（秒）。1s ≪ 段周期(≈10s)且 ≪ 缓冲容量(≈90s)。
        """
        self._snapshot_fn = snapshot_fn
        self._persist_fn = persist_fn
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="HLSSegmentSweeper"
        )
        self._thread.start()
        logger.info("[HLSSegmentSweeper] Started, interval=%.1fs", self.interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # 首次等待一个 interval，避免启动即空扫
        while not self._stop_event.wait(timeout=self.interval_seconds):
            try:
                self._sweep()
            except Exception:
                # L1 边界层：捕获扫描中一切未预期异常，记录后继续下一轮（保线程存活）
                logger.exception(
                    "[HLSSegmentSweeper] Unexpected error during sweep, will retry next interval"
                )

    def _sweep(self) -> None:
        """扫一遍活跃 CQ，把每个已攒满的整段拉走落盘。"""
        for task_id, cq in self._snapshot_fn().items():
            step_id = cq.step_id
            if step_id is None:
                continue  # 无 step_id（裸建/未绑定），无法定位落盘分区，跳过

            while (seg := cq.take_raw_segment()) is not None:
                self._enqueue(task_id, step_id, "raw", seg)
            while (seg := cq.take_processed_segment()) is not None:
                self._enqueue(task_id, step_id, "processed", seg)

    def _enqueue(
        self, task_id: int, step_id: int, segment_type: str, frames: List[Frame]
    ) -> None:
        self._persist_fn(
            task_id=task_id,
            step_id=step_id,
            segment_type=segment_type,
            frames=frames,
        )
