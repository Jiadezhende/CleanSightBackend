"""推理管理器 - 核心实现

基于推理服务架构改进方案（INFERENCE_SERVICE_IMPROVEMENT_PLAN.md）的完整实现。

架构特点：
1. 推理与可视化解耦：推理线程只负责推理，可视化异步执行
2. 时序分析独立：时序逻辑从推理线程中分离，支持复杂时序算法
3. 降帧可视化补偿：可视化使用最新原始帧 + 缓存的检测结果
4. 异步管道架构：推理 → 时序分析 → 可视化 → 写回，完全异步

数据流：
StageAwareDispatcher → InferWorker → TemporalWorkerPool → VisualizationWorkerPool → WriteBackWorkerPool
"""

import base64
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import cv2
import numpy as np

logger = logging.getLogger(__name__)

from app.models.frame import FrameData, ProcessedFrame
from app.models.task import Task as CleaningTask
from app.services.client import ClientQueues, client_manager
from app.services.inference.components.temporal_analyzer import DefaultTemporalAnalyzer
from app.services.inference.components.visualizer import DefaultVisualizer
from app.services.inference.core.service import ModelWorkerService
from app.services.inference.models import (
    InferenceResult,
    TemporalAnalysisPackage,
    TemporalAnalysisResult,
    WriteBackData,
)
from app.services.inference.workers.temporal import TemporalWorkerPool
from app.services.inference.workers.visualization import VisualizationWorkerPool
from app.services.inference.workers.writeback import WriteBackWorkerPool


class InferenceManager:
    """推理管理器 - 新架构实现

    集成：
    - ModelWorkerService（推理服务）
    - TemporalWorkerPool（时序分析）
    - VisualizationWorkerPool（可视化）
    - WriteBackWorkerPool（写回）

    特性：
    - 使用 ClientManager 统一管理客户端队列
    - 异步管道架构，推理、时序、可视化、写回完全解耦
    - 支持降帧推理 + 全帧率可视化
    - 保留原有 API 接口，无缝替换
    """

    def __init__(
        self,
        rt_fps: int = 30,
        ca_segment_seconds: int = 10,  # 改为10秒，即300帧
        db_dir: Optional[str] = None,
        ca_maxlen: int = 500,
        temporal_threads: int = 2,
        visualization_threads: int = 4,
        writeback_threads: int = 2,
    ):
        """初始化推理管理器。

        Args:
            rt_fps: 实时帧率
            ca_segment_seconds: CA 段长度（秒）
            db_dir: 数据库存储目录
            ca_maxlen: CA 队列最大长度
            temporal_threads: 时序分析线程数
            visualization_threads: 可视化线程数
            writeback_threads: 写回线程数
        """
        # 队列参数
        self._rt_maxlen = max(5, int(rt_fps))
        self._ca_segment_len = max(10, int(rt_fps * ca_segment_seconds))
        self._ca_maxlen = max(50, ca_maxlen)

        # 数据库存储目录
        base_dir = Path(__file__).parent.parent.parent.parent.parent.resolve()
        self._db_dir = Path(db_dir) if db_dir else base_dir / "database"
        self._db_dir.mkdir(parents=True, exist_ok=True)

        # 停止事件
        self._stop_event = threading.Event()

        # ========== 核心组件初始化 ==========

        # 1. 推理服务配置（延迟初始化，避免循环导入）
        self._stage_configs: Optional[Dict[str, Dict[str, Any]]] = None
        self._model_worker_service: Optional[ModelWorkerService] = None

        # ========== 异步管道架构 ==========
        logger.debug("[InferenceManager] Initializing async pipeline architecture")

        # 创建队列（负责各个worker池的通信）
        self.temporal_queue: "queue.Queue[InferenceResult]" = queue.Queue(maxsize=256)
        self.visualization_queue: "queue.Queue[TemporalAnalysisPackage]" = queue.Queue(
            maxsize=256
        )
        self.writeback_queue: "queue.Queue[WriteBackData]" = queue.Queue(maxsize=256)

        # 创建时序分析器
        temporal_config = self._create_temporal_config()
        self.temporal_analyzer = DefaultTemporalAnalyzer(config=temporal_config)

        # 创建可视化器
        self.visualizer = DefaultVisualizer()

        # 创建 Worker 池（各池管理自己的 stop_event）
        self.temporal_pool = TemporalWorkerPool(
            input_queue=self.temporal_queue,
            output_queue=self.visualization_queue,
            analyzer=self.temporal_analyzer,
            num_workers=temporal_threads,
        )

        self.visualization_pool = VisualizationWorkerPool(
            input_queue=self.visualization_queue,
            output_queue=self.writeback_queue,
            visualizer=self.visualizer,
            num_workers=visualization_threads,
        )

        self.writeback_pool = WriteBackWorkerPool(
            input_queue=self.writeback_queue,
            num_workers=writeback_threads,
            enable_db_write=False,  # 可选：是否写入数据库
        )

        # 自定义推理服务（将结果投递到时序队列）
        self._model_worker_service = self._create_async_model_worker_service()

        # ========== 持久化管理器（新架构） ==========
        from app.services.persistence import PersistenceConfig, PersistenceManager

        # 加载配置并覆盖storage路径
        persist_config = PersistenceConfig.from_yaml()
        persist_config.storage.base_dir = str(self._db_dir)
        self.persistence_manager = PersistenceManager(config=persist_config)

        # 持久化线程（仅用于段检查）
        self._segment_check_thread: Optional[threading.Thread] = None
        self._segment_check_interval: float = 1.0  # 检查间隔（秒）

        # 预编码缓存（避免重复编码同一帧）
        self._encoded_cache: Dict[str, Dict[str, Any]] = {}
        self._encoded_cache_lock = threading.Lock()

        # 客户端刷新线程（定期同步客户端列表）
        self._refresh_thread: Optional[threading.Thread] = None

        logger.debug("[InferenceManager] Initialization completed")

    def _get_stage_configs(self) -> Dict[str, Dict[str, Any]]:
        """延迟初始化 stage 配置（优先从配置文件加载）"""
        if self._stage_configs is None:
            try:
                # 尝试从配置文件加载（优先）
                from app.services.inference.components.component_factory import (
                    ComponentFactory,
                )
                from app.services.inference.config import load_stage_config

                config = load_stage_config()
                factory = ComponentFactory(config)

                # 为每个 stage 创建模型实例
                stage_configs = {}
                skipped_stages = []
                for stage_name in config.list_stages():
                    models = factory.create_models_for_stage(stage_name)
                    if models:  # 只添加有模型的 stage
                        stage_configs[stage_name] = {
                            "models": models,
                            "batch_size": config.batch_size,  # 从配置文件读取
                        }
                    else:
                        skipped_stages.append(stage_name)

                if stage_configs:
                    logger.info(
                        "[InferenceManager] Loaded %d stages (active): %s", 
                        len(stage_configs), 
                        list(stage_configs.keys())
                    )
                    if skipped_stages:
                        logger.info(
                            "[InferenceManager] Skipped %d stages (no models): %s",
                            len(skipped_stages),
                            skipped_stages
                        )
                    self._stage_configs = stage_configs
                else:
                    # 配置文件中没有可用的 stage，使用默认配置
                    logger.warning("[InferenceManager] No stages in config, using defaults")
                    from app.services.inference.core.factory import (
                        _create_default_stage_configs,
                    )

                    self._stage_configs = _create_default_stage_configs()
            except Exception as e:
                # 加载配置失败，回退到默认配置
                logger.error("[InferenceManager] Failed to load config: %s, using defaults", e)
                from app.services.inference.core.factory import (
                    _create_default_stage_configs,
                )

                self._stage_configs = _create_default_stage_configs()

        return self._stage_configs

    def _create_temporal_config(self) -> Dict[str, Dict[str, Any]]:
        """创建时序分析器配置（优先从配置文件加载）"""
        try:
            # 尝试从配置文件加载时序分析器配置
            from app.services.inference.config import load_stage_config

            config = load_stage_config()
            temporal_config = {}

            for stage_name in config.list_stages():
                stage_cfg = config.get_stage_config(stage_name)
                if stage_cfg and stage_cfg.temporal_analyzer:
                    # 提取时序分析器的配置
                    analyzer_cfg = stage_cfg.temporal_analyzer.get("config", {})
                    if analyzer_cfg:
                        temporal_config[stage_name] = analyzer_cfg

            if temporal_config:
                logger.debug("[InferenceManager] Loaded temporal analyzer config from file")
                return temporal_config
        except Exception as e:
            logger.debug("[InferenceManager] Failed to load temporal config: %s, using defaults", e)

        # 默认配置（回退）
        return {
            "LEAK": {
                "bubble": {
                    "mode": "consecutive",
                    "threshold": 3,  # 连续3帧
                },
                "bending": {
                    "mode": "sliding_window",
                    "window_seconds": 2.0,  # 2秒窗口
                    "ratio": 0.7,  # 70%比例
                },
            },
            "CLEAN": {
                "quality": {
                    "mode": "sliding_window",
                    "window_seconds": 2.0,
                    "ratio": 0.8,  # 80%比例
                },
            },
        }

    def _create_async_model_worker_service(self):
        """创建异步模式的推理服务（将结果投递到时序队列）"""
        from app.services.inference.core.service import ModelWorkerService

        # ModelWorkerService 现在直接支持异步模式
        service = ModelWorkerService(
            temporal_queue=self.temporal_queue,
            stage_configs=self._get_stage_configs(),
            max_batch_per_stage=8,
            use_cuda_stream=True,
            num_worker_threads=2,
        )

        return service

    # ========== 公共 API（保持与旧代码兼容） ==========

    def get_result(
        self, client_id: str, as_model: bool = False
    ) -> Union[None, FrameData, ProcessedFrame]:
        """返回最新处理帧（从 RT-ProcessedQueue）"""
        if not client_manager.has_client(client_id):
            return None

        cq = client_manager.get_client(client_id)
        if not cq:
            return None

        frame_data = cq.get_latest_result()
        if frame_data is None:
            return None

        if not as_model:
            return frame_data

        # 检查缓存：避免重复编码同一帧
        with self._encoded_cache_lock:
            cached = self._encoded_cache.get(client_id)
            if cached and cached["timestamp"] == frame_data.timestamp:
                task_id = cq.get_task_id()
                return ProcessedFrame(
                    task_id=task_id,
                    client_id=client_id,
                    raw_timestamp=datetime.fromtimestamp(frame_data.timestamp),
                    processed_frame_b64=cached["b64"],
                    inference_result=cached["inference_result"],
                )

        # 缓存未命中，编码并缓存
        task_id = cq.get_task_id()
        processed_frame = self._create_processed_frame(frame_data, task_id, client_id)

        # 更新缓存
        with self._encoded_cache_lock:
            self._encoded_cache[client_id] = {
                "timestamp": frame_data.timestamp,
                "b64": processed_frame.processed_frame_b64,
                "inference_result": processed_frame.inference_result,
            }

        return processed_frame

    def remove_client(self, client_id: str) -> None:
        """移除客户端的推理资源（不清理 ClientManager）

        职责：
        - 落盘残余缓存数据（保存已有的处理结果）
        - 刷新推理服务，移除过时的批次
        - 不负责清理 ClientManager（由 API 层或其他调用方负责）

        阶段1: 落盘残余缓存
        阶段2: 刷新推理服务
        """
        logger.info(f"[InferenceManager] Removing inference resources: {client_id}")

        # 阶段1: 落盘残余缓存（保存已有的处理结果）
        cq = (
            client_manager.get_client(client_id)
            if client_manager.has_client(client_id)
            else None
        )
        if cq:
            try:
                self._flush_all_remaining_segments(client_id, cq)
                logger.info(f"[InferenceManager] Segments flushed: {client_id}")
            except Exception as e:
                logger.warning(
                    f"[InferenceManager] Failed to flush segments: {client_id} - {e}"
                )
        else:
            logger.warning(
                f"[InferenceManager] Client not found in ClientManager, skipping flush: {client_id}"
            )

        # 阶段2: 刷新推理服务（冗余操作，保留向后兼容）
        # 注意：Dispatcher 现在实时同步客户端，此步骤技术上已非必需
        if self._model_worker_service is not None:
            try:
                self._model_worker_service.refresh_client_queues()
                logger.info(f"[InferenceManager] Worker service refreshed (redundant): {client_id}")
            except Exception as e:
                logger.error(
                    f"[InferenceManager] Failed to refresh worker service: {e}"
                )

        logger.info(f"[InferenceManager] Inference resources removed: {client_id}")

    def status(self) -> Dict[str, Any]:
        """获取所有客户端及其队列状态"""
        clients = client_manager.get_all_clients()
        stats = {client_id: cq.to_status_dict() for client_id, cq in clients.items()}
        return {"clients": len(clients), "queues": stats}

    def set_task(self, client_id: str, task: Optional[CleaningTask]) -> bool:
        """为客户端设置任务"""
        cq = client_manager.get_client(client_id)
        cq.set_task(task)
        return True

    def get_task(self, client_id: str) -> Optional[CleaningTask]:
        """获取客户端的任务"""
        cq = client_manager.get_client(client_id)
        return cq.get_task() if cq else None

    # ========== 内部辅助方法 ==========

    def _create_processed_frame(
        self, frame_data: FrameData, task_id: Optional[int], client_id: str
    ) -> ProcessedFrame:
        """从 FrameData 创建 ProcessedFrame 对象（含 Base64 编码）"""
        _, buf = cv2.imencode(".jpg", frame_data.frame)
        b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

        # 过滤推理结果，移除不可序列化的对象
        inference_result = self._make_json_serializable(
            frame_data.inference_result or {}
        )

        return ProcessedFrame(
            task_id=task_id,
            client_id=client_id,
            raw_timestamp=datetime.fromtimestamp(frame_data.timestamp),
            processed_frame_b64=b64,
            inference_result=inference_result,
        )

    def _make_json_serializable(self, obj: Any) -> Any:
        """递归过滤对象，移除不可 JSON 序列化的内容"""
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                # 跳过已知的不可序列化字段
                if key in ("annotated_frame", "processed_frame", "frame"):
                    continue
                result[key] = self._make_json_serializable(value)
            return result
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]
        elif isinstance(obj, np.ndarray):
            # numpy 数组转为列表（如果是小数组）
            if obj.size < 100:
                return obj.tolist()
            return None
        elif isinstance(obj, (np.integer, np.floating)):
            return obj.item()
        else:
            return obj

    # ========== HLS 段落盘逻辑（保留原有实现） ==========

    def _flush_segment_if_needed(self, client_id: str, client_queues: ClientQueues):
        """当队列达到阈值时，生成原始和处理后的 HLS 视频段及关键点 JSON。

        检查阈值并将待写盘数据放入持久化队列，由持久化线程执行实际写盘/上报/落库工作。

        策略：ca_raw 和 ca_processed 独立落盘（因为积累速度不同）
        """
        seg_len = client_queues.ca_segment_len
        ca_raw_len = client_queues.get_ca_raw_length()
        ca_processed_len = client_queues.get_ca_processed_length()

        # 调试日志：显示队列状态
        if ca_raw_len >= seg_len or ca_processed_len >= seg_len:
            logger.debug(
                "[SegmentCheck] client=%s: ca_raw=%d/%d, ca_processed=%d/%d",
                client_id, ca_raw_len, seg_len, ca_processed_len, seg_len
            )

        # 1. 检查 ca_raw 是否需要落盘（独立）
        if ca_raw_len >= seg_len:
            logger.info(
                "[SegmentCheck] Enqueueing RAW segment persist job for client: %s, len=%d",
                client_id, seg_len
            )
            self._enqueue_raw_segment_job(client_id, client_queues, seg_len)

        # 2. 检查 ca_processed 是否需要落盘（独立）
        if ca_processed_len >= seg_len:
            logger.info(
                "[SegmentCheck] Enqueueing PROCESSED segment persist job for client: %s, len=%d",
                client_id, seg_len
            )
            self._enqueue_processed_segment_job(client_id, client_queues, seg_len)

    def _segment_check_loop(self):
        """周期性检查所有客户端队列，触发分段落盘。"""
        logger.info("[SegmentCheck] Segment check thread started")
        while not self._stop_event.is_set():
            try:
                # 获取所有客户端
                clients = client_manager.get_all_clients()

                # 检查每个客户端是否需要分段落盘
                for client_id, cq in clients.items():
                    try:
                        self._flush_segment_if_needed(client_id, cq)
                    except Exception as e:
                        logger.error(f"[SegmentCheck] Error checking client {client_id}: {e}")

                # 等待下一次检查
                time.sleep(self._segment_check_interval)
            except Exception as e:
                logger.error(f"[SegmentCheck] Segment check loop error: {e}")

        logger.info("[SegmentCheck] Segment check thread stopped")

    def _flush_all_remaining_segments(
        self, client_id: str, client_queues: ClientQueues
    ) -> None:
        """在任务/客户端结束时，将剩余缓存全部落盘（独立处理raw和processed）"""
        try:
            seg_len = client_queues.ca_segment_len

            # 1. 处理 ca_raw 队列的所有剩余帧（分批落盘）
            while client_queues.get_ca_raw_length() >= seg_len:
                self._enqueue_raw_segment_job(client_id, client_queues, seg_len)

            # 处理 ca_raw 的残余（不足一个段长）
            remaining_raw = client_queues.get_ca_raw_length()
            if remaining_raw > 0:
                self._enqueue_raw_segment_job(client_id, client_queues, remaining_raw)

            # 2. 处理 ca_processed 队列的所有剩余帧（分批落盘）
            while client_queues.get_ca_processed_length() >= seg_len:
                self._enqueue_processed_segment_job(client_id, client_queues, seg_len)

            # 处理 ca_processed 的残余（不足一个段长）
            remaining_processed = client_queues.get_ca_processed_length()
            if remaining_processed > 0:
                self._enqueue_processed_segment_job(
                    client_id, client_queues, remaining_processed
                )

        except Exception as e:
            logger.error(f"_flush_all_remaining_segments error for {client_id}: {e}")

    def _enqueue_raw_segment_job(
        self, client_id: str, client_queues: ClientQueues, seg_len: int
    ) -> None:
        """仅落盘 raw 帧（使用新的PersistenceManager）"""
        task_id = client_queues.get_task_id()
        if task_id is None:
            print(
                f"[InferenceManager] Warning: task_id is None for client {client_id}, skipping raw segment persist"
            )
            return

        raw_frames_data: List[FrameData] = client_queues.pop_n_ca_raw(seg_len)
        if not raw_frames_data:
            return

        # 使用新的persistence_manager
        self.persistence_manager.persist_hls_segment(
            client_id=client_id,
            task_id=task_id,
            segment_type="raw",
            frames=raw_frames_data,
        )

    def _enqueue_processed_segment_job(
        self, client_id: str, client_queues: ClientQueues, seg_len: int
    ) -> None:
        """仅落盘 processed 帧（使用新的PersistenceManager）"""
        task_id = client_queues.get_task_id()
        if task_id is None:
            print(
                f"[InferenceManager] Warning: task_id is None for client {client_id}, skipping processed segment persist"
            )
            return

        processed_frames_data: List[FrameData] = client_queues.pop_n_ca_processed(
            seg_len
        )
        if not processed_frames_data:
            return

        # 使用新的persistence_manager
        self.persistence_manager.persist_hls_segment(
            client_id=client_id,
            task_id=task_id,
            segment_type="processed",
            frames=processed_frames_data,
        )

    # ========== 告警处理逻辑（独立于HLS） ==========

    def enqueue_alarm(self, alarm_info: Dict[str, Any]):
        """将告警信息入队，使用新的PersistenceManager处理。

        外部调用接口（Pipeline、时序分析等模块可调用此方法触发告警）

        Args:
            alarm_info: 告警信息字典，应包含:
                - task_id: 任务ID
                - step_id: 步骤ID
                - client_id: 客户端ID
                - detection_result: 检测结果（可选）
                - alarm_type: 告警类型（可选）
                - alarm_message: 告警消息（可选）
        """
        # 直接委托给persistence_manager
        self.persistence_manager.persist_alarm(alarm_info)

    # ========== 刷新客户端列表 ==========

    def _client_refresh_loop(self):
        """定期刷新客户端列表（保留作为冗余检查）
        
        注意：
        - Dispatcher 已改为直接引用 ClientManager，客户端变化实时同步
        - 此线程保留仅作为冗余检查和日志统计
        - 可在后续验证稳定后移除
        """
        logger.info("[InferenceManager] Client refresh thread started (redundant check mode)")
        while not self._stop_event.is_set():
            try:
                if self._model_worker_service:
                    self._model_worker_service.refresh_client_queues()
                time.sleep(5)  # 每 5 秒刷新一次
            except Exception as e:
                logger.error("[InferenceManager] Client refresh error: %s", e, exc_info=True)

    # ========== 启动/停止 ==========

    def start(self):
        """启动推理管理器"""
        logger.info("[InferenceManager] 启动中...")

        # 1. 启动推理服务
        if self._model_worker_service:
            self._model_worker_service.start()

        # 2. 启动异步管道
        self.temporal_pool.start()
        self.visualization_pool.start()
        self.writeback_pool.start()

        # 3. 启动分段检查线程（周期性检查队列并触发落盘）
        if (
            self._segment_check_thread is None
            or not self._segment_check_thread.is_alive()
        ):
            self._segment_check_thread = threading.Thread(
                target=self._segment_check_loop, daemon=True, name="SegmentCheckThread"
            )
            self._segment_check_thread.start()

        # 4. 启动持久化管理器（新架构）
        self.persistence_manager.start()

        # 5. 启动客户端刷新线程（可选，冗余检查）
        # 注意：Dispatcher 已改为实时同步，此线程保留仅作统计和兜底
        if self._refresh_thread is None or not self._refresh_thread.is_alive():
            self._refresh_thread = threading.Thread(
                target=self._client_refresh_loop,
                daemon=True,
                name="ClientRefreshThread",
            )
            self._refresh_thread.start()

        logger.info("[InferenceManager] Started")

    def stop(self):
        """停止推理管理器"""
        logger.info("[InferenceManager] Stopping...")

        # 设置停止事件
        self._stop_event.set()

        # 1. 停止推理服务
        if self._model_worker_service:
            self._model_worker_service.stop()

        # 2. 停止异步管道
        self.temporal_pool.stop()
        self.visualization_pool.stop()
        self.writeback_pool.stop()

        # 3. 停止持久化管理器（新架构）
        self.persistence_manager.stop(timeout=10.0)

        # 4. 停止分段检查线程
        if self._segment_check_thread is not None:
            try:
                self._segment_check_thread.join(timeout=2.0)
            except Exception:
                pass

        # 5. 停止客户端刷新线程
        if self._refresh_thread is not None:
            try:
                self._refresh_thread.join(timeout=2.0)
            except Exception:
                pass

        logger.info("[InferenceManager] Stopped")
