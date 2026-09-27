"""
持久化管理器 - 统一调度所有持久化任务

职责：
- 管理告警 Worker Pool 与存储 TTL 清理 Worker
- 接收告警持久化请求并入队
- 监控持久化队列和性能指标

HLS 落盘不在本服务：写侧是 `app.services.recording` → `app.storage.hls`。
"""

import logging
import queue
from typing import Any, Dict, Optional

from .config import PersistenceConfig
from .types import AlarmPersistenceTask
from .workers.alarm_worker import AlarmWorkerPool
from .workers.cleanup_worker import StorageCleanupWorker

logger = logging.getLogger(__name__)


class PersistenceManager:
    """持久化管理器 - 中央调度器"""

    def __init__(self, config: Optional[PersistenceConfig] = None):
        """初始化持久化管理器

        Args:
            config: 持久化配置（如未提供则使用单例配置）
        """
        if config is None:
            from .config import get_persistence_config

            self.config = get_persistence_config()
        else:
            self.config = config

        self.alarm_queue: queue.Queue[AlarmPersistenceTask] = queue.Queue(
            maxsize=self.config.alarm_queue_size
        )

        self.alarm_pool = AlarmWorkerPool(
            input_queue=self.alarm_queue,
            num_workers=self.config.alarm_workers,
        )

        # 存储清理 Worker（按配置条件创建）
        self._cleanup_worker: StorageCleanupWorker | None = None
        if self.config.enable_cleanup:
            self._cleanup_worker = StorageCleanupWorker(
                db_dir=self.config.storage_base_dir,
                cleanup_days=self.config.cleanup_days,
                interval_seconds=self.config.cleanup_interval_seconds,
            )

    def start(self):
        """启动持久化服务（告警池 + TTL 清理）。"""
        logger.info("启动持久化服务")
        self.alarm_pool.start()
        if self._cleanup_worker:
            self._cleanup_worker.start()

    def stop(self, timeout: float = 10.0):
        """停止持久化服务（优雅关闭）。"""
        logger.info("停止持久化服务")

        # 停止Worker池（会等待队列清空）
        self.alarm_pool.stop(timeout=timeout)
        if self._cleanup_worker:
            self._cleanup_worker.stop(timeout=5.0)

    # ========== 告警持久化API ==========

    def persist_alarm(self, alarm_info: Dict[str, Any]) -> bool:
        """持久化告警信息（支持批量去重）

        Args:
            alarm_info: 告警信息字典

        Returns:
            是否成功入队
        """
        try:
            task = AlarmPersistenceTask.from_dict(alarm_info)
            self.alarm_queue.put(task, timeout=0.5)
            return True
        except queue.Full:
            logger.warning("告警队列已满")
            return False
        except Exception as e:
            logger.error("告警入队失败: %s", e, exc_info=True)
            return False
