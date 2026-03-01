"""推理管理器 - 核心实现

架构特点：
1. 推理与可视化解耦：推理线程只负责推理，可视化独立定时拉取
2. 时序分析独立：时序逻辑从推理线程中分离，支持复杂时序算法
3. 三池独立时钟：推理、时序分析、可视化各自独立节奏，不通过队列串联
4. 双写 + 原子快照：推理结果同时写入 slide_window（历史）和 latest_inference（最新快照）

数据流：
InferenceLoop → cq.push_detection() + cq.set_latest_inference()  [双写]
TemporalWorker (1Hz)  → cq.get_slide_window() → analyze → cq.set_latest_temporal()
VisualizationWorker (~15Hz) → cq.get_latest_inference() + get_latest_frame() + get_latest_temporal() → render → cq
"""

import base64
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

import cv2
import numpy as np

logger = logging.getLogger(__name__)

from app.models.frame import FrameData, ProcessedFrame
from app.models.task import Task as CleaningTask
from app.services.client import ClientQueues, client_manager
from app.services.inference.core.service import ModelWorkerService
from app.services.inference.workers.temporal import TemporalWorkerPool
from app.services.inference.workers.visualization import VisualizationWorkerPool


class InferenceManager:
    """推理管理器

    集成三个独立时钟的 Worker 池：
    - ModelWorkerService（推理，~30 FPS）
    - TemporalWorkerPool（时序分析，1 Hz）
    - VisualizationWorkerPool（可视化，~15 FPS）

    三池通过 ClientQueues 上的原子槽位通信，不通过队列串联。
    """

    def __init__(
        self,
        rt_fps: int = 30,
        ca_segment_seconds: int = 10,  # 改为10秒，即300帧
        db_dir: Optional[str] = None,
        ca_maxlen: int = 500,
    ):
        """初始化推理管理器。

        Args:
            rt_fps: 实时帧率
            ca_segment_seconds: CA 段长度（秒）
            db_dir: 数据库存储目录
            ca_maxlen: CA 队列最大长度
        """
        # 队列参数
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

        # ========== 三池独立架构（无队列串联） ==========
        logger.debug("[InferenceManager] Initializing independent worker pools")

        # TemporalWorkerPool: 定时轮询模式，独立时钟
        self.temporal_pool = TemporalWorkerPool(
            num_workers=1,  # 单线程轮询足够，时序分析无 GPU 开销
            stage_configs=None,  # 将在 start() 后设置
            tick_interval=1.0,
        )

        # VisualizationWorkerPool: 定时拉取模式，独立时钟
        self.visualization_pool = VisualizationWorkerPool(
            target_fps=15,
            stage_configs=None,  # 将在 start() 后设置
        )

        # 推理服务（结果同步进 ClientQueues.slide_window，不再投递到队列）
        self._model_worker_service = self._create_async_model_worker_service()

        # ========== 持久化管理器（全局单例，与 client_manager 模式一致） ==========
        from app.services.persistence import persistence_manager as _persistence_manager

        # 将 db_dir 覆盖应用到持久化配置和已创建的 HLS 策略（防止退化为 yaml 默认值）
        _persistence_manager.config.storage.base_dir = str(self._db_dir)
        _persistence_manager.hls_pool.strategy.db_dir = self._db_dir
        self.persistence_manager = _persistence_manager

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
                from app.services.inference.stage_factory import StageFactory
                from app.services.inference.config import load_stage_config

                config = load_stage_config()
                factory = StageFactory(config)

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
                    # 配置文件中没有可用的 stage，抛出错误
                    raise ValueError(
                        "No valid stages found in configuration file. "
                        "Please ensure inference_config.yaml contains at least one stage with valid models."
                    )
            except Exception as e:
                # 加载配置失败，抛出错误
                logger.error("[InferenceManager] Failed to load config: %s", e)
                raise RuntimeError(
                    f"Failed to load inference configuration: {e}. "
                    "Please check inference_config.yaml and ensure it is properly configured."
                ) from e

        return self._stage_configs

    def _create_async_model_worker_service(self):
        """创建推理服务（结果同步进 ClientQueues.slide_window）"""
        from app.services.inference.core.service import ModelWorkerService

        service = ModelWorkerService(
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

        # 落盘残余缓存（保存已有的处理结果）
        cq = (
            client_manager.get_client(client_id)
            if client_manager.has_client(client_id)
            else None
        )
        if cq:
            try:
                self._flush_all_remaining_segments(client_id, cq)
                logger.info(f"[InferenceManager] Segments WroteBack: {client_id}")
            except Exception as e:
                logger.warning(
                    f"[InferenceManager] Failed to write back segments: {client_id} - {e}"
                )
        else:
            logger.warning(
                f"[InferenceManager] Client not found in ClientManager, skipping writeback: {client_id}"
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

    # ========== HLS 段落盘逻辑 ==========

    def _flush_all_remaining_segments(
        self, client_id: str, client_queues: ClientQueues
    ) -> None:
        """在任务/客户端结束时，将剩余缓存全部落盘。

        使用 drain 方法原子排空队列，避免与 append_ca_raw/processed 的并发竞态。
        """
        try:
            task_id = client_queues.get_task_id()
            if task_id is None:
                logger.warning(
                    "[InferenceManager] task_id is None for %s, skipping flush",
                    client_id,
                )
                return

            seg_len = client_queues.ca_segment_len

            # 原子排空（线程安全，排空后 append 不会再触发自动落盘）
            raw_frames = client_queues.drain_ca_raw()
            processed_frames = client_queues.drain_ca_processed()

            # 分段落盘 raw
            for i in range(0, len(raw_frames), seg_len):
                chunk = raw_frames[i : i + seg_len]
                if chunk:
                    self.persistence_manager.persist_hls_segment(
                        client_id=client_id,
                        task_id=task_id,
                        segment_type="raw",
                        frames=chunk,
                    )

            # 分段落盘 processed
            for i in range(0, len(processed_frames), seg_len):
                chunk = processed_frames[i : i + seg_len]
                if chunk:
                    self.persistence_manager.persist_hls_segment(
                        client_id=client_id,
                        task_id=task_id,
                        segment_type="processed",
                        frames=chunk,
                    )

        except Exception as e:
            logger.error(f"_flush_all_remaining_segments error for {client_id}: {e}")

    # ========== 告警处理逻辑（独立于HLS） ==========

    def enqueue_alarm(self, alarm_info: Dict[str, Any]):
        """将告警信息入队，使用新的PersistenceManager处理。

        外部调用接口（Pipeline、时序分析等模块可调用此方法触发告警）

        Args:
            alarm_info: 告警信息字典，应包含:
                - task_id: 任务ID
                - stage: 阶段名称
                - client_id: 客户端ID
                - detection_result: 检测结果（可选）
                - alarm_type: 告警类型（可选）
                - alarm_message: 告警消息（可选）
        """
        # 直接委托给persistence_manager
        self.persistence_manager.persist_alarm(alarm_info)

    # ========== 启动/停止 ==========

    def start(self):
        """启动推理管理器"""
        logger.info("[InferenceManager] 启动中...")

        # 1. 启动推理服务
        if self._model_worker_service:
            self._model_worker_service.start()

        # 1.5 设置异步管道的 stage_configs（新架构）
        if hasattr(self, '_model_worker_service') and self._model_worker_service:
            stage_configs = self._get_stage_configs()
            self.temporal_pool.stage_configs = stage_configs
            self.visualization_pool.stage_configs = stage_configs

        # 2. 启动异步管道
        self.temporal_pool.start()
        self.visualization_pool.start()

        # 3. 启动持久化管理器（新架构）
        self.persistence_manager.start()

        logger.info("[InferenceManager] Started")

    def stop(self):
        """停止推理管理器"""

        # 设置停止事件
        self._stop_event.set()

        # 1. 停止推理服务
        if self._model_worker_service:
            self._model_worker_service.stop()

        # 2. 停止异步管道
        self.temporal_pool.stop()
        self.visualization_pool.stop()

        # 3. 停止持久化管理器（新架构）
        self.persistence_manager.stop(timeout=10.0)

        # 4. 停止客户端刷新线程
        if self._refresh_thread is not None:
            try:
                self._refresh_thread.join(timeout=2.0)
            except Exception:
                pass

        logger.info("[InferenceManager] Stopped")
