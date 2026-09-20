"""段拉取的**节拍器**：每隔一个 interval，对每个活跃 CQ 调一次 `service.collect_from`。

**它只管什么时候拉，不管拉什么**——取哪几条队列、按什么顺序取、断流残帧怎么办，全在
`RecordingService.collect_from`。本线程是运行期 CQ 的唯一 drain 者，别处直接调
`collect_from` / `flush_residual` 会让入队顺序出现竞态（后果见 `service` 模块 docstring
的不变式 2）。

**包内私有**：只由 `RecordingService.start()` 构造，启停归服务。

依赖上界：stdlib。不 import 任何单例——`clients` 与 `service` 都是注入的。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class SegmentSweeper:
    """周期性触发录制服务去各个活跃 CQ 取帧。"""

    def __init__(self, clients, service, interval_seconds: float = 1.0):
        """
        Args:
            clients: 提供 `snapshot()` 的 CQ 注册表（= `client_manager`）。
            service: 每个 CQ 交给谁去取（= `RecordingService`，只用它的 `collect_from`）。
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
        """对每个活跃 CQ 调一次 `collect_from`。

        **`cq` 整个传过去**，不拆成 task_id/step_id：代次身份就是这个对象引用，必须在
        **取帧的那一刻**捕获。晚一步去注册表里取，取到的可能已经是新一代的 CQ，这批帧
        就会被记到别人名下。
        """
        for cq in self._clients.snapshot().values():
            self._service.collect_from(cq)
