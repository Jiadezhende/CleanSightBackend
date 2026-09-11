"""段拉取线程（PULL 模型）—— 周期扫活跃 CQ，把已攒满的整段拉走交给 `RecordingService`。

**包内私有**（前导下划线）：它只由 `RecordingService.start()` 构造，外面拿到它没有意义
——启停归服务，拉段的结果也只进服务的队列。

为什么是 PULL 不是 PUSH：分段判定与落盘触发是录制的职责，`ClientQueues` 该退回纯缓冲
容器。历史上 `append_ca_*` 里直接调落盘，等于让缓冲区知道存储的事。

运行期只落**整段**；末尾不足一段的残帧由 `RecordingService.flush_residual` 在拆除时收尾。

依赖上界：stdlib + `app.domain`。不 import 任何单例——`clients` 与 `service` 都是注入的。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class SegmentSweeper:
    """周期性从活跃 CQ 拉取攒满的 HLS 整段。"""

    def __init__(self, clients, service, interval_seconds: float = 1.0):
        """
        Args:
            clients: 提供 `snapshot()` 的 CQ 注册表（= `client_manager`）。
            service: 拉到段之后交给谁（= `RecordingService`，用它的 `submit_segment`）。
            interval_seconds: 扫描间隔（秒）。1s ≪ 段周期(≈10s) 且 ≪ 缓冲容量(≈90s)。
        """
        self._clients = clients
        self._service = service
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="RecordingSweeper"
        )
        self._thread.start()
        logger.info("[recording.sweeper] 已启动, interval=%.1fs", self.interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        # 首次等一个 interval，避免启动即空扫
        while not self._stop_event.wait(timeout=self.interval_seconds):
            try:
                self._sweep()
            except Exception:
                # 边界层：捕获扫描中一切未预期异常，记录后继续下一轮（保线程存活）
                logger.exception("[recording.sweeper] 扫描异常，下一轮重试")

    def _sweep(self) -> None:
        """扫一遍活跃 CQ，把每个已攒满的整段拉走提交。

        **`cq` 整个交给 `submit_segment`**，不是拆成 task_id/step_id 再传：代次身份就是
        这个对象引用，必须在**取帧的那一刻**捕获。晚一步去注册表里取，取到的可能已经是
        新一代的 CQ，这批帧就会被记到别人名下。
        """
        for cq in self._clients.snapshot().values():
            if cq.step_id is None:
                # 裸建 / 未绑定 step：定位不到落盘分区。**不取帧**——取了就只能丢，
                # 留在缓冲里等它绑上 step 才是对的。
                continue
            while (seg := cq.take_raw_segment()) is not None:
                self._service.submit_segment(cq, "raw", seg)
            while (seg := cq.take_processed_segment()) is not None:
                self._service.submit_segment(cq, "processed", seg)
