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
from typing import Any, Dict, List, Optional

from app.domain.frame import Frame
from app.services.persistence.config import PersistenceConfig
from app.services.persistence.models import (
    AlarmPersistenceTask,
    HLSPersistenceTask,
)
from app.services.persistence.workers.alarm_worker import AlarmWorkerPool
from app.services.persistence.workers.cleanup_worker import StorageCleanupWorker
from app.services.persistence.workers.hls_worker import HLSWorkerPool
from app.services.persistence.workers.segment_sweeper import HLSSegmentSweeper

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

        # 存储清理 Worker（按配置条件创建）
        self._cleanup_worker: StorageCleanupWorker | None = None
        if self.config.enable_cleanup:
            self._cleanup_worker = StorageCleanupWorker(
                db_dir=self.config.storage_base_dir,
                cleanup_days=self.config.cleanup_days,
                interval_seconds=self.config.cleanup_interval_seconds,
            )

        # HLS 分段拉取 Worker（PULL 模型）：周期扫活跃 CQ 把攒满的整段拉走落盘。
        # 注入 client_manager.snapshot + 本管理器的 persist_hls_segment；
        # 依赖方向 persistence→client 单向（client 不再回指 persistence）。
        from app.services.client.manager import client_manager

        self._segment_sweeper = HLSSegmentSweeper(
            snapshot_fn=client_manager.snapshot,
            persist_fn=self.persist_hls_segment,
            interval_seconds=self.config.hls_sweep_interval_seconds,
        )

    def start(self):
        """启动持久化服务"""
        logger.info("启动持久化服务")
        self.hls_pool.start()
        self.alarm_pool.start()
        self._segment_sweeper.start()
        if self._cleanup_worker:
            self._cleanup_worker.start()

    def stop(self, timeout: float = 10.0):
        """停止持久化服务（优雅关闭）"""
        logger.info("停止持久化服务")

        # 先停 sweeper（不再拉新段），残段由 RunController 拆除时 flush；
        # 再停 Worker 池，保证 sweeper 已入队的整段仍被消费落盘。
        self._segment_sweeper.stop(timeout=5.0)

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
            return True
        except queue.Full:
            logger.warning(
                "HLS队列已满，丢弃任务: task_id=%s step_id=%s", task_id, step_id
            )
            return False
        except Exception as e:
            logger.error("HLS入队失败: %s", e, exc_info=True)
            return False

    def flush_residual_segments(self, cq) -> None:
        """拆除期落盘 CQ 残余帧：drain raw/processed → 按 ca_segment_len 切段 → 逐段入队。

        task_id/step_id 由 cq 派生（缺失早退）。须在 cq.close() 释放帧之前调（RunController 保证）。
        （原 InferenceManager._flush_all_remaining_segments 迁入——切段是持久化领域知识。）
        """
        task_id = cq.task_id
        if task_id is None:
            logger.warning("[persistence] flush_residual_segments: task_id is None, skip")
            return
        step_id = cq.step_id
        if step_id is None:
            logger.error(
                "[persistence] flush_residual_segments: step_id is None (task_id=%s), skip", task_id
            )
            return

        seg_len = cq.ca_segment_len
        raw_frames = cq.drain_ca_raw()
        processed_frames = cq.drain_ca_processed()

        for i in range(0, len(raw_frames), seg_len):
            chunk = raw_frames[i : i + seg_len]
            if chunk:
                self.persist_hls_segment(
                    task_id=task_id, step_id=step_id, segment_type="raw", frames=chunk
                )

        for i in range(0, len(processed_frames), seg_len):
            chunk = processed_frames[i : i + seg_len]
            if chunk:
                self.persist_hls_segment(
                    task_id=task_id, step_id=step_id, segment_type="processed", frames=chunk
                )

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

    def persist_alarms(
        self,
        alarms: List,
        *,
        cq,
        client_id: str,
        mode: str,
        log_each: bool = False,
    ) -> None:
        """把一批告警过闸门后落库 + 记入内存环形缓冲（实时/结算共用一条映射）。

        别名已由产出方（temporal actor）烧进 alarm.stage，此处直接读、不反向 import inference.naming；
        metric 直接读 alarm.metric（产出方已填）；task_id/step_id 由 cq 派生。
        顺序先内存后外部：内存日志供前端实时轮询，外部库本就 30s 批次。
        （原 inference/temporal/alarm_sink.persist_alarms 迁入——告警落库是持久化领域。）
        """
        task_id = cq.task_id
        step_id = cq.step_id

        for alarm in alarms:
            # 给产出方的同一份告警补 mode，再过闸门+入环形缓冲（seq 由其赋；stage 已烧）。
            # 闸门 task_id 取自 cq 自身不可变身份，无需再传。
            alarm.mode = mode
            if not cq.append_alarm_record_with_gate(alarm, mode):
                continue
            self.persist_alarm({
                "task_id": task_id,
                "stage": alarm.stage,
                "step_id": step_id,
                "client_id": client_id,
                "alarm_type": alarm.alarm_type,
                "alarm_metric": alarm.metric,
                "alarm_mode": mode,
                "alarm_level": alarm.alarm_level,
                "alarm_message": alarm.alarm_message,
                "detection_result": alarm.metadata if alarm.metadata else None,
            })
            if log_each:
                logger.info(
                    "[persistence] %s alarm for %s: %s", mode, client_id, alarm.alarm_message
                )

    def release_task_locks(self, task_id: int) -> None:
        """任务拆除后回收该 task 的 HLS 目录锁（防 _dir_locks 随任务数无限增长）。

        由 RunController.stop_run 在清 registry 之后调用——此时不会再有该 task 的新段入队。
        """
        self.hls_pool.release_dir_locks(task_id)
