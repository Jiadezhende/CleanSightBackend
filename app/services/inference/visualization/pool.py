"""pool.py - 可视化线程池（VisualizationWorkerPool）。

线程管理职责：按 target_fps 启停单条 VisualizationWorker 线程。
不持有渲染逻辑（在 visualizer.py）、不持有拉取循环（在 worker.py）。
"""

import logging
import threading
from typing import Any, Dict, Optional

from app.services.inference.visualization.worker import VisualizationWorker
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class VisualizationWorkerPool:
    """可视化线程池（定时拉取模式，单线程）。

    单线程理由：单帧渲染 ~5ms，@20FPS 时 tick 预算 50ms，约可覆盖 10 clients；
    单线程避免了多线程竞争同一客户端的问题。
    实际 target_fps 由调用方注入 settings.inference_fps——viz 是"采样后 inference 流"的
    消费者，poll 率跟随该流速率（渲染按 inference.ts 去重，快则空转、慢则丢帧），属合法
    派生而非共用旋钮；HLS processed 打标已改走 eff_fps、不再共享此值。默认值仅作脱离装配时的兜底。
    """

    def __init__(
        self,
        target_fps: float = 20,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化线程池。

        Args:
            target_fps: 目标可视化帧率（装配时由 settings.inference_fps 注入）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.target_fps = target_fps
        self.stage_configs = stage_configs or {}

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def start(self):
        """启动工作线程。"""
        tick_interval = 1.0 / self.target_fps

        worker = VisualizationWorker(
            stop_event=self._stop_event,
            tick_interval=tick_interval,
            worker_id=0,
            stage_configs=self.stage_configs,
        )

        self._worker_thread = threading.Thread(
            target=guarded_run,
            args=(worker.run, self._stop_event, "VisualizationWorker-0"),
            daemon=True,
            name="VisualizationWorker-0",
        )
        self._worker_thread.start()

        logger.info(
            "[VisualizationWorkerPool] Started (target_fps=%.0f, tick=%.3fs)",
            self.target_fps, tick_interval,
        )

    def stop(self):
        """停止工作线程。"""
        self._stop_event.set()

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)

        logger.debug("[VisualizationWorkerPool] Stopped")
