"""
持久化管理器 - 统一调度所有持久化任务

职责：
- 管理持久化Worker Pool（HLS、告警）
- 接收持久化请求并路由到对应Worker
- 提供统一的持久化API
- 监控持久化队列和性能指标
"""

import logging
import queue
import threading
from typing import Any, Dict, List, Optional

from app.domain.frame import Frame
from app.services.persistence.config import PersistenceConfig
from app.services.persistence.models import (
    AlarmPersistenceTask,
    HLSPersistenceTask,
    PersistenceMetrics,
)
from app.services.persistence.workers.alarm_worker import AlarmWorkerPool
from app.services.persistence.workers.cleanup_worker import StorageCleanupWorker
from app.services.persistence.workers.hls_worker import HLSWorkerPool

logger = logging.getLogger(__name__)


class PersistenceManager:
    """持久化管理器 - 中央调度器"""

    def __init__(self, config: Optional[PersistenceConfig] = None):
        """初始化持久化管理器

        Args:
            config: 持久化配置（如未提供则使用单例配置）
        """
        if config is None:
            from app.services.persistence.config import get_persistence_config

            self.config = get_persistence_config()
        else:
            self.config = config

        # 停止事件
        self._stop_event = threading.Event()

        # 创建持久化队列
        self.hls_queue: queue.Queue[HLSPersistenceTask] = queue.Queue(
            maxsize=self.config.hls_queue_size
        )
        self.alarm_queue: queue.Queue[AlarmPersistenceTask] = queue.Queue(
            maxsize=self.config.alarm_queue_size
        )

        # 创建Worker池
        self.hls_pool = HLSWorkerPool(
            input_queue=self.hls_queue,
            num_workers=self.config.hls_workers,
            db_dir=self.config.storage_base_dir,
            segment_duration=self.config.segment_duration,
            raw_fps=self.config.raw_fps,
            processed_fps=self.config.processed_fps,
        )

        self.alarm_pool = AlarmWorkerPool(
            input_queue=self.alarm_queue,
            num_workers=self.config.alarm_workers,
        )

        # 监控指标
        self.metrics = PersistenceMetrics()

        # 存储清理 Worker（按配置条件创建）
        self._cleanup_worker: StorageCleanupWorker | None = None
        if self.config.enable_cleanup:
            self._cleanup_worker = StorageCleanupWorker(
                db_dir=self.config.storage_base_dir,
                cleanup_days=self.config.cleanup_days,
                interval_seconds=self.config.cleanup_interval_seconds,
            )

    def start(self):
        """启动持久化服务"""
        logger.info("启动持久化服务")
        self.hls_pool.start()
        self.alarm_pool.start()
        if self._cleanup_worker:
            self._cleanup_worker.start()

    def stop(self, timeout: float = 10.0):
        """停止持久化服务（优雅关闭）"""
        logger.info("停止持久化服务")
        self._stop_event.set()

        # 停止Worker池（会等待队列清空）
        self.hls_pool.stop(timeout=timeout)
        self.alarm_pool.stop(timeout=timeout)
        if self._cleanup_worker:
            self._cleanup_worker.stop(timeout=5.0)

    # ========== HLS持久化API ==========

    def persist_hls_segment(
        self,
        task_id: int,
        step_id: int,
        segment_type: str,  # "raw" or "processed"
        frames: List[Frame],
    ) -> bool:
        """持久化HLS视频段

        Args:
            task_id: 任务ID
            step_id: 洗消步骤ID（来自 clean_task.current_step 转 int）
            segment_type: 段类型（raw/processed）
            frames: 帧数据列表

        Returns:
            是否成功入队
        """
        try:
            task = HLSPersistenceTask(
                task_id=task_id,
                step_id=step_id,
                segment_type=segment_type,
                frames=frames,
            )
            self.hls_queue.put(task, timeout=1.0)
            self.metrics.hls_enqueued += 1
            return True
        except queue.Full:
            self.metrics.hls_queue_full += 1
            logger.warning(
                "HLS队列已满，丢弃任务: task_id=%s step_id=%s", task_id, step_id
            )
            return False
        except Exception as e:
            self.metrics.hls_errors += 1
            logger.error("HLS入队失败: %s", e, exc_info=True)
            return False

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
            self.metrics.alarm_enqueued += 1
            return True
        except queue.Full:
            self.metrics.alarm_queue_full += 1
            logger.warning("告警队列已满")
            return False
        except Exception as e:
            self.metrics.alarm_errors += 1
            logger.error("告警入队失败: %s", e, exc_info=True)
            return False

    # ========== 监控API ==========

    def get_metrics(self) -> Dict[str, Any]:
        """获取持久化指标"""
        return {
            "hls_queue_size": self.hls_queue.qsize(),
            "alarm_queue_size": self.alarm_queue.qsize(),
            "hls_enqueued": self.metrics.hls_enqueued,
            "hls_completed": self.metrics.hls_completed,
            "hls_errors": self.metrics.hls_errors,
            "hls_queue_full": self.metrics.hls_queue_full,
            "alarm_enqueued": self.metrics.alarm_enqueued,
            "alarm_completed": self.metrics.alarm_completed,
            "alarm_errors": self.metrics.alarm_errors,
            "alarm_queue_full": self.metrics.alarm_queue_full,
        }

    def flush_remaining(self, client_id: str):
        """刷新客户端的所有待持久化数据（任务结束时调用）"""
        # 通知Worker池刷新特定客户端的数据
        self.hls_pool.flush_client(client_id)
