"""
告警服务 - 统一调度告警上报任务

职责：
- 管理告警 Worker Pool
- 接收告警上报请求并入队
- 监控告警队列和性能指标

HLS 落盘不在本服务：写侧是 `app.services.recording` → `app.storage.hls`。
"""

import logging
import queue
from typing import Any, Dict, Optional

from .alarm_worker import AlarmWorkerPool
from .config import AlarmServiceConfig
from .types import AlarmReportTask

logger = logging.getLogger(__name__)


class AlarmService:
    """告警服务 - 中央调度器"""

    def __init__(self, config: Optional[AlarmServiceConfig] = None):
        """初始化告警服务

        Args:
            config: 告警服务配置（如未提供则使用单例配置）
        """
        if config is None:
            from .config import get_alarm_config

            self.config = get_alarm_config()
        else:
            self.config = config

        self.alarm_queue: queue.Queue[AlarmReportTask] = queue.Queue(
            maxsize=self.config.alarm_queue_size
        )

        self.alarm_pool = AlarmWorkerPool(
            input_queue=self.alarm_queue,
            num_workers=self.config.alarm_workers,
        )

    def start(self):
        """启动告警服务（告警池）。"""
        logger.info("[AlarmService] 启动告警服务")
        self.alarm_pool.start()

    def stop(self, timeout: float = 10.0):
        """停止告警服务（优雅关闭）。"""
        logger.info("[AlarmService] 停止告警服务")

        # 停止Worker池（会等待队列清空）
        self.alarm_pool.stop(timeout=timeout)

    # ========== 告警上报API ==========

    def persist_alarm(self, alarm_info: Dict[str, Any]) -> bool:
        """告警入队等待上报

        Args:
            alarm_info: 告警信息字典

        Returns:
            是否成功入队
        """
        try:
            task = AlarmReportTask.from_dict(alarm_info)
            self.alarm_queue.put(task, timeout=0.5)
            return True
        except queue.Full:
            logger.warning("[AlarmService] 告警队列已满")
            return False
        except Exception as e:
            logger.error("[AlarmService] 告警入队失败: %s", e, exc_info=True)
            return False
