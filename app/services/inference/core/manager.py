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
import json
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

from app.database import engine
from app.models.frame import FrameData, ProcessedFrame
from app.models.task import Task as CleaningTask
from app.services.client import ClientQueues, client_manager
from app.services.inference.core.factory import create_model_worker_service_from_manager
from app.services.inference.models import (
    InferenceResult,
    TemporalAnalysisPackage,
    TemporalAnalysisResult,
    WriteBackData,
)
from app.services.inference.core.service import ModelWorkerService
from app.services.inference.components.temporal_analyzer import DefaultTemporalAnalyzer
from app.services.inference.workers.temporal import TemporalWorkerPool
from app.services.inference.components.visualizer import DefaultVisualizer
from app.services.inference.workers.visualization import (
    VisualizationWorkerPool,
)
from app.services.inference.workers.writeback import WriteBackWorkerPool
from app.settings import settings

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
        use_async_pipeline: bool = True,  # 是否启用异步管道
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
            use_async_pipeline: 是否启用异步管道（新架构）
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

        # 异步管道开关
        self.use_async_pipeline = use_async_pipeline

        # 停止事件
        self._stop_event = threading.Event()

        # ========== 核心组件初始化 ==========

        # 1. 推理服务配置（延迟初始化，避免循环导入）
        self._stage_configs: Optional[Dict[str, Dict[str, Any]]] = None
        self._model_worker_service: Optional[ModelWorkerService] = None

        if use_async_pipeline:
            # ========== 异步管道模式 ==========
            print("[InferenceManager] 启用异步管道架构")

            # 创建队列（负责各个worker池的通信）
            self.temporal_queue: "queue.Queue[InferenceResult]" = queue.Queue(maxsize=256)
            self.visualization_queue: "queue.Queue[TemporalAnalysisPackage]" = queue.Queue(maxsize=256)
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

        else:
            # ========== 同步模式（兼容旧代码） ==========
            print("[InferenceManager] 使用同步模式（兼容旧代码）")
            self._model_worker_service = create_model_worker_service_from_manager(
                stage_configs=self._get_stage_configs(),
                max_batch_per_stage=8,
                use_cuda_stream=True,
                num_worker_threads=2,
            )

        # 持久化队列与线程（HLS 段写盘，存储了不同客户端的HLS待落盘）
        self._persist_queue: "queue.Queue" = queue.Queue()
        self._persist_thread: Optional[threading.Thread] = None
        self._segment_check_thread: Optional[threading.Thread] = None  # 队列分段检查线程
        self._segment_check_interval: float = 1.0  # 检查间隔（秒）

        # 告警相关（独立队列，不与HLS混合）
        self._alarm_queue: "queue.Queue" = queue.Queue()  # 告警持久化队列（独立）
        self._alarm_persist_thread: Optional[threading.Thread] = None  # 告警持久化线程
        self._alarm_lock = threading.Lock()
        self._pending_alarms: Dict[str, Dict[str, Any]] = {}  # 待处理告警（去重用）
        self._recent_alarms: Dict[str, float] = {}  # 最近上报的告警（冷却用）
        self._alarm_batch_interval = getattr(settings, "alarm_batch_interval", 30)
        self._alarm_cooldown_seconds = getattr(settings, "alarm_cooldown_seconds", 60)
        self._alarm_flush_thread: Optional[threading.Thread] = None  # 告警批量flush线程

        # 预编码缓存（避免重复编码同一帧）
        self._encoded_cache: Dict[str, Dict[str, Any]] = {}
        self._encoded_cache_lock = threading.Lock()

        # 客户端刷新线程（定期同步客户端列表）
        self._refresh_thread: Optional[threading.Thread] = None

        print("[InferenceManager] 初始化完成")

    def _get_stage_configs(self) -> Dict[str, Dict[str, Any]]:
        """延迟初始化 stage 配置（优先从配置文件加载）"""
        if self._stage_configs is None:
            try:
                # 尝试从配置文件加载（优先）
                from app.services.inference.config_loader import load_stage_config
                from app.services.inference.components.component_factory import ComponentFactory

                config = load_stage_config()
                factory = ComponentFactory(config)

                # 为每个 stage 创建模型实例
                stage_configs = {}
                for stage_name in config.list_stages():
                    models = factory.create_models_for_stage(stage_name)
                    if models:  # 只添加有模型的 stage
                        stage_configs[stage_name] = {
                            "models": models,
                            "batch_size": 4,  # 默认batch size
                        }

                if stage_configs:
                    print(f"[InferenceManager] 从配置文件加载 {len(stage_configs)} 个 stage")
                    self._stage_configs = stage_configs
                else:
                    # 配置文件中没有可用的 stage，使用默认配置
                    print("[InferenceManager] 配置文件中没有可用 stage，使用默认配置")
                    from app.services.inference.core.factory import _create_default_stage_configs
                    self._stage_configs = _create_default_stage_configs()
            except Exception as e:
                # 加载配置失败，回退到默认配置
                print(f"[InferenceManager] 加载配置文件失败: {e}，使用默认配置")
                from app.services.inference.core.factory import _create_default_stage_configs
                self._stage_configs = _create_default_stage_configs()

        return self._stage_configs

    def _create_temporal_config(self) -> Dict[str, Dict[str, Any]]:
        """创建时序分析器配置（优先从配置文件加载）"""
        try:
            # 尝试从配置文件加载时序分析器配置
            from app.services.inference.config_loader import load_stage_config

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
                print(f"[InferenceManager] 从配置文件加载时序分析器配置")
                return temporal_config
        except Exception as e:
            print(f"[InferenceManager] 加载时序配置失败: {e}，使用默认配置")

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

    def _create_async_model_worker_service(self) -> ModelWorkerService:
        """创建异步模式的推理服务（将结果投递到时序队列）"""
        # 创建服务（延迟初始化配置）
        service = create_model_worker_service_from_manager(
            stage_configs=self._get_stage_configs(),
            max_batch_per_stage=8,
            use_cuda_stream=True,
            num_worker_threads=2,
        )

        # 覆写结果回写方法，将结果投递到时序队列
        original_write_back = service._write_back_results

        def async_write_back(results: List[InferenceResult]):
            """异步回写：将结果投递到时序队列，而非直接写入 ClientQueues"""
            for res in results:
                try:
                    if not client_manager.has_client(res.client_id):
                        continue

                    # 保存原始帧（供可视化使用）
                    cq = client_manager.get_client(res.client_id)
                    if cq:
                        latest_frame = cq.get_latest_frame()
                        if latest_frame is not None:
                            res.frame = latest_frame  # 保存最新帧

                    # 投递到时序分析队列
                    self.temporal_queue.put(res, timeout=0.1)
                except queue.Full:
                    print(f"[InferenceManager] 时序队列已满，丢弃结果: {res.client_id}")
                except Exception as e:
                    print(f"[InferenceManager] 投递时序队列异常: {e}")

        # 替换回写方法
        service._write_back_results = async_write_back  # type: ignore

        return service

    # ========== 公共 API（保持与旧代码兼容） ==========

    def submit_frame(self, client_id: str, frame: np.ndarray) -> None:
        """提交原始帧到 CA-ReadyQueue（拉流层调用）"""
        now = time.time()
        frame_data = FrameData(timestamp=now, frame=frame)

        cq = client_manager.get_client(
            client_id,
            rt_maxlen=self._rt_maxlen,
            ca_segment_len=self._ca_segment_len,
            ca_maxlen=self._ca_maxlen,
        )
        cq.append_ca_ready(frame_data)

    def set_rtmp_url(self, client_id: str, rtmp_url: str) -> None:
        """设置客户端的 RTMP 流地址"""
        cq = client_manager.get_client(client_id)
        cq.rtmp_url = rtmp_url

    def set_stream_url(self, client_id: str, stream_url: str) -> None:
        """设置客户端的通用流地址（RTMP/RTSP）"""
        cq = client_manager.get_client(client_id)
        cq.rtmp_url = stream_url

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
        """优雅地移除客户端（三阶段停止）

        阶段1: 落盘已有的处理结果
        阶段2: 从 ClientManager 移除并刷新推理服务
        阶段3: 正在处理的批次完成后会被自动丢弃
        """
        logger.debug(f"[InferenceManager] Removing client: {client_id}")

        if not client_manager.has_client(client_id):
            logger.debug(f"[InferenceManager] Client not found (already removed): {client_id}")
            return

        logger.info(f"[InferenceManager] Cleaning up client: {client_id}")

        # 阶段1: 落盘剩余缓存（保存已有的处理结果）
        cq = client_manager.get_client(client_id)
        if cq:
            try:
                self._flush_all_remaining_segments(client_id, cq)
            except Exception as e:
                logger.warning(f"[InferenceManager] Failed to flush segments: {client_id} - {e}")

        # 阶段2: 从 ClientManager 移除
        try:
            client_manager.remove_client(client_id)
        except Exception as e:
            logger.error(f"[InferenceManager] Failed to remove from ClientManager: {client_id} - {e}")

        # 阶段3: 刷新推理服务列表（此后推理完成的帧会被丢弃）
        if self._model_worker_service is not None:
            try:
                self._model_worker_service.refresh_client_queues()
            except Exception as e:
                logger.error(f"[InferenceManager] Failed to refresh worker service: {e}")

        logger.info(f"[InferenceManager] Client cleanup complete: {client_id}")

    def status(self) -> Dict[str, Any]:
        """获取所有客户端及其队列状态"""
        clients = client_manager.get_all_clients()
        stats = {
            client_id: cq.to_status_dict() for client_id, cq in clients.items()
        }
        return {"clients": len(clients), "queues": stats}

    def set_task(self, client_id: str, task: Optional[CleaningTask]) -> bool:
        """为客户端设置任务"""
        cq = client_manager.get_client(client_id)
        cq.set_task(task)
        print(f"任务已设置 for client {client_id}: {task}")
        return True

    def get_task(self, client_id: str) -> Optional[CleaningTask]:
        """获取客户端的任务"""
        cq = client_manager.get_client(client_id)
        return cq.get_task() if cq else None

    def terminate_task_by_id(self, client_id: str) -> bool:
        """终止指定客户端的任务，清理所有队列和资源"""
        cq = client_manager.get_client(client_id)
        if cq is None:
            return False

        # 落盘剩余缓存
        try:
            self._flush_all_remaining_segments(client_id, cq)
        except Exception as e:
            print(f"终止任务 {client_id} 时落盘失败: {e}")

        # 清理队列
        cq.clear()

        # 从 ClientManager 移除
        try:
            client_manager.remove_client(client_id)
        except Exception:
            pass

        print(f"任务已终止，客户端 {client_id} 的所有队列和资源已清理")
        return True

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
        ca_raw_len = len(client_queues.ca_raw)
        ca_processed_len = len(client_queues.ca_processed)

        # 调试日志：显示队列状态
        if ca_raw_len >= seg_len or ca_processed_len >= seg_len:
            print(f"[SegmentCheck] client={client_id}: ca_raw={ca_raw_len}/{seg_len}, ca_processed={ca_processed_len}/{seg_len}")

        # 1. 检查 ca_raw 是否需要落盘（独立）
        if ca_raw_len >= seg_len:
            raw_len = min(ca_raw_len, seg_len)
            print(f"[SegmentCheck] Enqueueing RAW segment persist job for client: {client_id}, len={raw_len}")
            self._enqueue_raw_segment_job(client_id, client_queues, raw_len)

        # 2. 检查 ca_processed 是否需要落盘（独立）
        if ca_processed_len >= seg_len:
            processed_len = min(ca_processed_len, seg_len)
            print(f"[SegmentCheck] Enqueueing PROCESSED segment persist job for client: {client_id}, len={processed_len}")
            self._enqueue_processed_segment_job(client_id, client_queues, processed_len)

    def _segment_check_loop(self):
        """周期性检查所有客户端队列，触发分段落盘。"""
        print("[SegmentCheck] Segment check thread started")
        while not self._stop_event.is_set():
            try:
                # 获取所有客户端
                clients = client_manager.get_all_clients()

                # 检查每个客户端是否需要分段落盘
                for client_id, cq in clients.items():
                    try:
                        self._flush_segment_if_needed(client_id, cq)
                    except Exception as e:
                        print(f"[SegmentCheck] Error checking client {client_id}: {e}")

                # 等待下一次检查
                time.sleep(self._segment_check_interval)
            except Exception as e:
                print(f"[SegmentCheck] Segment check loop error: {e}")

        print("[SegmentCheck] Segment check thread stopped")

    def _flush_all_remaining_segments(
        self, client_id: str, client_queues: ClientQueues
    ) -> None:
        """在任务/客户端结束时，将剩余缓存全部落盘（独立处理raw和processed）"""
        try:
            seg_len = client_queues.ca_segment_len

            # 1. 处理 ca_raw 队列的所有剩余帧（分批落盘）
            while len(client_queues.ca_raw) >= seg_len:
                self._enqueue_raw_segment_job(client_id, client_queues, seg_len)

            # 处理 ca_raw 的残余（不足一个段长）
            remaining_raw = len(client_queues.ca_raw)
            if remaining_raw > 0:
                self._enqueue_raw_segment_job(client_id, client_queues, remaining_raw)

            # 2. 处理 ca_processed 队列的所有剩余帧（分批落盘）
            while len(client_queues.ca_processed) >= seg_len:
                self._enqueue_processed_segment_job(client_id, client_queues, seg_len)

            # 处理 ca_processed 的残余（不足一个段长）
            remaining_processed = len(client_queues.ca_processed)
            if remaining_processed > 0:
                self._enqueue_processed_segment_job(client_id, client_queues, remaining_processed)

        except Exception as e:
            print(f"_flush_all_remaining_segments error for {client_id}: {e}")

    def _enqueue_raw_segment_job(
        self, client_id: str, client_queues: ClientQueues, seg_len: int
    ) -> None:
        """仅落盘 raw 帧（独立落盘）"""
        task_id = client_queues.get_task_id()
        hls_dir = self._db_dir / client_id / str(task_id)
        hls_dir.mkdir(parents=True, exist_ok=True)

        raw_frames_data: List[FrameData] = client_queues.pop_n_ca_raw(seg_len)
        if not raw_frames_data:
            return

        try:
            self._persist_queue.put(
                {
                    "type": "raw_segment",
                    "client_id": client_id,
                    "target_dir": hls_dir,
                    "raw_frames": raw_frames_data,
                }
            )
        except Exception as e:
            print(f"Failed to enqueue raw segment persist job for {client_id}: {e}")

    def _enqueue_processed_segment_job(
        self, client_id: str, client_queues: ClientQueues, seg_len: int
    ) -> None:
        """仅落盘 processed 帧（独立落盘）"""
        task_id = client_queues.get_task_id()
        hls_dir = self._db_dir / client_id / str(task_id)
        hls_dir.mkdir(parents=True, exist_ok=True)

        processed_frames_data: List[FrameData] = client_queues.pop_n_ca_processed(seg_len)
        if not processed_frames_data:
            return

        try:
            self._persist_queue.put(
                {
                    "type": "processed_segment",
                    "client_id": client_id,
                    "target_dir": hls_dir,
                    "processed_frames": processed_frames_data,
                }
            )
        except Exception as e:
            print(f"Failed to enqueue processed segment persist job for {client_id}: {e}")

    def _persistent_worker(self):
        """persistanc 消费线程：处理写盘 HLS 段与告警上报/落库等耗时操作，job结构见 enqueue 部分"""
        print("Persistent worker started")
        while not self._stop_event.is_set():
            try:
                job = None
                try:
                    job = self._persist_queue.get(timeout=1.0)
                except Exception:
                    job = None

                if job is None:
                    continue

                jtype = job.get("type")
                if jtype == "raw_segment":
                    print("[Persistent worker] Processing raw segment job")
                    try:
                        client_id = job.get("client_id")
                        target_dir = job.get("target_dir")
                        raw_frames = job.get("raw_frames", [])
                        self._do_persist_raw_segment(client_id, target_dir, raw_frames)
                    except Exception as e:
                        print(f"Persistent worker raw segment job failed: {e}")
                elif jtype == "processed_segment":
                    print("[Persistent worker] Processing processed segment job")
                    try:
                        client_id = job.get("client_id")
                        target_dir = job.get("target_dir")
                        processed_frames = job.get("processed_frames", [])
                        self._do_persist_processed_segment(client_id, target_dir, processed_frames)
                    except Exception as e:
                        print(f"Persistent worker processed segment job failed: {e}")
                elif jtype == "alarm":
                    print("[Persistent worker] Processing alarm job")
                    try:
                        alarm = job.get("alarm")
                        self._handle_alarm_now(alarm)
                    except Exception as e:
                        print(f"Persistent worker alarm job failed: {e}")
                else:
                    print(f"Persistent worker unknown job type: {jtype}")

            except Exception as e:
                print(f"Persistent worker loop error: {e}")

        print("Persistent worker stopped")

    def _do_persist_raw_segment(
        self,
        client_id: str,
        hls_dir: Path,
        raw_frames_data: List[FrameData],
    ):
        """落盘原始视频段（独立）"""
        if hls_dir is None:
            print(f"_do_persist_raw_segment error: hls_dir is None for client {client_id}")
            return
        if not raw_frames_data:
            print(f"_do_persist_raw_segment error: empty frames for client {client_id}")
            return

        try:
            start_ts = raw_frames_data[0].timestamp

            # 生成原始视频段（使用原始视频源帧率30fps）
            raw_segment_path = hls_dir / f"raw_segment_{int(start_ts * 1e6)}.mp4"
            height, width = raw_frames_data[0].frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore
            raw_fps = 30.0  # 原始视频保持30fps
            out_raw = cv2.VideoWriter(str(raw_segment_path), fourcc, raw_fps, (width, height))
            for fd in raw_frames_data:
                out_raw.write(fd.frame)
            out_raw.release()

            # 更新播放列表（使用实际时间戳计算时长，而非帧数/fps）
            raw_playlist_path = hls_dir / "raw_playlist.m3u8"
            # 使用第一帧和最后一帧的实际时间戳差值
            if len(raw_frames_data) > 1:
                actual_duration = raw_frames_data[-1].timestamp - raw_frames_data[0].timestamp
                # 加上最后一帧的持续时间（1/fps）
                segment_duration = actual_duration + (1.0 / raw_fps)
            else:
                segment_duration = 1.0 / raw_fps

            if not raw_playlist_path.exists():
                with raw_playlist_path.open("w") as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
            with raw_playlist_path.open("a") as f:
                f.write(f"#EXTINF:{segment_duration:.3f},\n")
                f.write(f"{raw_segment_path.name}\n")

            print(f"[Persistent worker] Raw segment persisted: {client_id}, frames={len(raw_frames_data)}, actual_duration={segment_duration:.3f}s, fps={raw_fps}")

        except Exception as e:
            print(f"_do_persist_raw_segment error: {e}")

    def _do_persist_processed_segment(
        self,
        client_id: str,
        hls_dir: Path,
        processed_frames_data: List[FrameData],
    ):
        """落盘处理后视频段和keypoints JSON（独立）"""
        if hls_dir is None:
            print(f"_do_persist_processed_segment error: hls_dir is None for client {client_id}")
            return
        if not processed_frames_data:
            print(f"_do_persist_processed_segment error: empty frames for client {client_id}")
            return

        try:
            start_ts = processed_frames_data[0].timestamp

            # 生成处理后视频段（使用推理帧率）
            segment_path = hls_dir / f"processed_segment_{int(start_ts * 1e6)}.mp4"
            height, width = processed_frames_data[0].frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore
            processed_fps = float(settings.inference_fps)
            out_processed = cv2.VideoWriter(
                str(segment_path), fourcc, processed_fps, (width, height)
            )
            for fd in processed_frames_data:
                out_processed.write(fd.frame)
            out_processed.release()

            # 写 keypoints JSON
            keypoints_path = hls_dir / f"keypoints_{int(start_ts * 1e6)}.json"
            keypoints_list = []
            for fd in processed_frames_data:
                kp = fd.keypoints if hasattr(fd, "keypoints") else None
                ir = fd.inference_result if hasattr(fd, "inference_result") else None
                keypoints_list.append(
                    {
                        "timestamp": fd.timestamp,
                        "keypoints": self._make_json_serializable(kp),
                        "inference_result": self._make_json_serializable(ir),
                    }
                )
            with keypoints_path.open("w", encoding="utf-8") as f:
                json.dump(keypoints_list, f, ensure_ascii=False, indent=2)

            # 更新播放列表（使用实际时间戳计算时长，而非帧数/fps）
            playlist_path = hls_dir / "processed_playlist.m3u8"
            # 使用第一帧和最后一帧的实际时间戳差值
            if len(processed_frames_data) > 1:
                actual_duration = processed_frames_data[-1].timestamp - processed_frames_data[0].timestamp
                # 加上最后一帧的持续时间（1/fps）
                segment_duration = actual_duration + (1.0 / processed_fps)
            else:
                segment_duration = 1.0 / processed_fps

            if not playlist_path.exists():
                with playlist_path.open("w") as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
            with playlist_path.open("a") as f:
                f.write(f"#EXTINF:{segment_duration:.3f},\n")
                f.write(f"{segment_path.name}\n")

            print(f"[Persistent worker] Processed segment persisted: {client_id}, frames={len(processed_frames_data)}, actual_duration={segment_duration:.3f}s, fps={processed_fps}")

        except Exception as e:
            print(f"_do_persist_processed_segment error: {e}")

    # ========== 告警处理逻辑（独立于HLS） ==========

    def enqueue_alarm(self, alarm_info: Dict[str, Any]):
        """将告警信息入队，交由批量去重线程处理。

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
        try:
            key = f"{alarm_info.get('task_id')}_{alarm_info.get('step_id')}"
            with self._alarm_lock:
                if key not in self._pending_alarms:
                    self._pending_alarms[key] = {
                        'count': 1,
                        'first_seen': time.time(),
                        'last_seen': time.time(),
                        'alarm_info': alarm_info
                    }
                else:
                    self._pending_alarms[key]['count'] += 1
                    self._pending_alarms[key]['last_seen'] = time.time()
        except Exception as e:
            print(f"[AlarmManager] enqueue_alarm error: {e}")

    def _flush_pending_alarms(self):
        """检查并上报待处理的告警（去重/批量逻辑）。

        策略：
        - 每次 flush 遍历 pending_alarms
        - 如果最近已上报且处于冷却期（_alarm_cooldown_seconds），则保留在 pending
        - 否则将该告警聚合（添加 count/first_seen/last_seen）并提交到告警队列
        """
        now = time.time()
        to_send = []
        with self._alarm_lock:
            keys = list(self._pending_alarms.keys())
            for key in keys:
                item = self._pending_alarms.get(key)
                if not item:
                    continue
                last_sent = self._recent_alarms.get(key)
                if last_sent and (now - last_sent) < self._alarm_cooldown_seconds:
                    # 仍在冷却期，跳过（保留在 pending）
                    continue
                # 准备发送：构建聚合告警信息
                agg = dict(item['alarm_info']) if item.get('alarm_info') else {}
                agg['alarm_count'] = item.get('count', 1)
                agg['first_seen'] = datetime.fromtimestamp(item.get('first_seen')).strftime("%Y-%m-%d %H:%M:%S")  # type: ignore
                agg['last_seen'] = datetime.fromtimestamp(item.get('last_seen')).strftime("%Y-%m-%d %H:%M:%S")  # type: ignore
                to_send.append((key, agg))
                # 更新最近上报时间并移除 pending
                self._recent_alarms[key] = now
                del self._pending_alarms[key]

        # 在锁外发送，避免阻塞其他操作
        for key, agg_alarm in to_send:
            try:
                # 将告警入独立的告警持久化队列
                self._alarm_queue.put(agg_alarm)
                print(f"[AlarmManager] 告警已入队: {key}, count={agg_alarm.get('alarm_count')}")
            except Exception as e:
                print(f"[AlarmManager] Failed to enqueue aggregated alarm for {key}: {e}")

    def _alarm_flush_loop(self):
        """后台线程，周期性 flush pending alarms。"""
        print("[AlarmManager] Alarm flush thread started")
        while not self._stop_event.is_set():
            try:
                time.sleep(self._alarm_batch_interval)
                self._flush_pending_alarms()
            except Exception as e:
                print(f"[AlarmManager] Alarm flush loop error: {e}")
        # 在退出前再 flush 一次（尝试上报剩余告警）
        try:
            self._flush_pending_alarms()
        except Exception as e:
            print(f"[AlarmManager] Final alarm flush error: {e}")
        print("[AlarmManager] Alarm flush thread stopped")

    def _alarm_persist_worker(self):
        """告警持久化线程：处理告警上报与数据库记录（独立于HLS）"""
        print("[AlarmManager] Alarm persist worker started")
        while not self._stop_event.is_set():
            try:
                alarm = None
                try:
                    alarm = self._alarm_queue.get(timeout=1.0)
                except Exception:
                    alarm = None

                if alarm is None:
                    continue

                # 执行告警上报与数据库记录
                try:
                    self._handle_alarm_now(alarm)
                except Exception as e:
                    print(f"[AlarmManager] Alarm persist worker failed: {e}")

            except Exception as e:
                print(f"[AlarmManager] Alarm persist worker loop error: {e}")

        print("[AlarmManager] Alarm persist worker stopped")

    def _handle_alarm_now(self, alarm_info: Dict[str, Any]):
        """实际执行告警上报与写库的函数（在告警持久化线程中调用）。

        参考原有 ai_backup 中的实现。
        """
        try:
            task_id = alarm_info.get('task_id')
            step_id = alarm_info.get('step_id')
            client_id = alarm_info.get('client_id')

            # 若 task_id/step_id 缺失，可尝试从 client queue 补全
            if (task_id is None or step_id is None) and client_id:
                try:
                    cq = client_manager.get_client(str(client_id))
                    if cq:
                        print(f"[AlarmManager] found client queue for {client_id}, task={cq.task}")
                    else:
                        print(f"[AlarmManager] no client queue found for {client_id}")
                    if cq and cq.task:
                        if task_id is None:
                            task_id = getattr(cq.task, 'task_id', None)
                        if step_id is None:
                            step_id = getattr(cq.task, 'current_step', None)
                except Exception as e:
                    print(f"[AlarmManager] Failed to fill task_id/step_id from client queues: {e}")

            print(f"[AlarmManager] final task_id={task_id}, step_id={step_id}, client_id={client_id}")
            detection_result = alarm_info.get('detection_result')

            alarm_type = alarm_info.get('alarm_type', '流程违规' if detection_result else '推理异常')
            alarm_level = alarm_info.get('alarm_level', 'high')
            alarm_message = alarm_info.get('alarm_message', 'AI推理检测到异常' if detection_result else 'AI推理异常')

            # 调用远端告警上报（仅在 task_id 和 step_id 可用时上报）
            try:
                alarm_count = alarm_info.get('alarm_count') if isinstance(alarm_info, dict) else None
                first_seen = alarm_info.get('first_seen') if isinstance(alarm_info, dict) else None
                last_seen = alarm_info.get('last_seen') if isinstance(alarm_info, dict) else None
                if task_id and step_id:
                    self._send_alarm_report(
                        task_id=task_id,
                        step_id=step_id,
                        alarm_type=alarm_type,
                        alarm_level=alarm_level,
                        alarm_message=alarm_message,
                        detection_result=detection_result,
                        camera_ip=None,
                        reader_ip=None,
                        alarm_count=alarm_count,
                        first_seen=first_seen,
                        last_seen=last_seen
                    )
                else:
                    print(f"[AlarmManager] Skipping remote alarm report: missing task_id or step_id (task_id={task_id}, step_id={step_id})")
            except Exception as e:
                print(f"[AlarmManager] Remote alarm report failed: {e}")

            # 写入本地数据库表 alarm_record（如果不存在则先创建）
            try:
                self._record_alarm_db(
                    task_id=task_id,
                    step_id=step_id,
                    alarm_type=alarm_type,
                    alarm_level=alarm_level,
                    alarm_message=alarm_message,
                    detection_result=detection_result
                )
            except Exception as e:
                print(f"[AlarmManager] Local DB alarm record failed: {e}")

        except Exception as e:
            print(f"[AlarmManager] _handle_alarm_now exception: {e}")

    def _send_alarm_report(
        self,
        task_id: int,
        step_id: int,
        alarm_type: str,
        alarm_level: str,
        alarm_message: str,
        detection_result: Optional[Dict] = None,
        camera_ip: Optional[str] = None,
        reader_ip: Optional[str] = None,
        alarm_count: Optional[int] = None,
        first_seen: Optional[str] = None,
        last_seen: Optional[str] = None
    ) -> Optional[bool]:
        """按照外部接口文档上报告警（同步调用，故需在后台线程使用）。

        增强：支持重试与可选的聚合字段（alarm_count, first_seen, last_seen）。
        """
        import urllib.request

        url = getattr(settings, 'alarm_report_url', "http://116.204.65.72:8881/gdmp/v1/api/nt/alarm_report")
        payload = {
            "task_id": task_id if task_id is not None else 0,
            "step_id": step_id if step_id is not None else 0,
            "alarm_type": alarm_type,
            "alarm_level": alarm_level,
            "alarm_message": alarm_message,
            "alarm_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        if detection_result is not None:
            payload['detection_result'] = detection_result
        if camera_ip:
            payload['camera_ip'] = camera_ip
        if reader_ip:
            payload['reader_ip'] = reader_ip
        # 聚合字段（可选）
        if alarm_count is not None:
            payload['alarm_count'] = int(alarm_count)
        if first_seen:
            payload['first_seen'] = first_seen
        if last_seen:
            payload['last_seen'] = last_seen

        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'AI-Backend/1.0'
        }

        # 重试逻辑
        max_retries = 3
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_text = resp.read().decode('utf-8')
                    try:
                        j = json.loads(resp_text)
                        if j.get('code') == 0:
                            print(f"[AlarmManager] Alarm reported successfully: task_id={task_id}")
                            return True
                        else:
                            print(f"[AlarmManager] Alarm report returned non-zero code: {j}")
                            return False
                    except Exception:
                        print(f"[AlarmManager] Alarm report response (non-json): {resp_text}")
                        return False
            except Exception as e:
                print(f"[AlarmManager] Attempt {attempt} failed to send alarm report: {e}")
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                else:
                    return False
        return False

    def _record_alarm_db(
        self,
        task_id: Optional[int],
        step_id: Optional[Any],
        alarm_type: str,
        alarm_level: str,
        alarm_message: str,
        detection_result: Optional[Dict] = None,
        camera_ip: Optional[str] = None,
        reader_ip: Optional[str] = None
    ) -> None:
        """在数据库中创建表 `clean_alarm`（若不存在）并插入一条告警记录。

        使用最新字段属性：
        - alarm_id (SERIAL PRIMARY KEY)
        - task_id INTEGER
        - step_id INTEGER
        - alarm_type TEXT
        - message TEXT
        - severity TEXT
        - resolved BOOLEAN
        - resolved_by INTEGER
        - detected_at BIGINT
        - resolved_at BIGINT
        """
        from sqlalchemy import text

        try:
            create_sql = '''
            CREATE TABLE IF NOT EXISTS clean_alarm (
                alarm_id SERIAL PRIMARY KEY,
                task_id INTEGER,
                step_id INTEGER,
                alarm_type TEXT,
                message TEXT,
                severity TEXT,
                resolved BOOLEAN DEFAULT FALSE,
                resolved_by INTEGER,
                detected_at BIGINT,
                resolved_at BIGINT,
                created_at TIMESTAMP DEFAULT now()
            )
            '''
            with engine.begin() as conn:
                conn.execute(text(create_sql))
                insert_sql = '''
                INSERT INTO clean_alarm (task_id, step_id, alarm_type, message, severity, resolved, resolved_by, detected_at, resolved_at)
                VALUES (:task_id, :step_id, :alarm_type, :message, :severity, :resolved, :resolved_by, :detected_at, :resolved_at)
                '''
                params = {
                    'task_id': int(task_id) if task_id is not None else None,
                    'step_id': int(step_id) if step_id is not None and str(step_id).isdigit() else None,
                    'alarm_type': alarm_type,
                    'message': alarm_message,
                    'severity': alarm_level,
                    'resolved': False,
                    'resolved_by': None,
                    'detected_at': int(time.time()),
                    'resolved_at': None
                }
                conn.execute(text(insert_sql), params)
                print(f"[AlarmManager] Alarm recorded to DB: task_id={task_id}, type={alarm_type}")
        except Exception as e:
            print(f"[AlarmManager] _record_alarm_db error: {e}")
            # 尝试回退：根据实际表结构动态构建 INSERT（保留原有的健壮性逻辑）
            try:
                with engine.connect() as conn2:
                    info_sql = text("SELECT column_name, column_default, is_nullable FROM information_schema.columns WHERE table_name = 'clean_alarm'")
                    res = conn2.execute(info_sql).fetchall()
                    print("[AlarmManager] clean_alarm table columns:")
                    cols = {}
                    for row in res:
                        print(row)
                        cols[row[0]] = {
                            'default': row[1],
                            'nullable': row[2]
                        }

                    # 只插入目标参数中存在于表中的列
                    candidate_params = {
                        'task_id': int(task_id) if task_id is not None else None,
                        'step_id': int(step_id) if step_id is not None and str(step_id).isdigit() else None,
                        'alarm_type': alarm_type,
                        'message': alarm_message,
                        'severity': alarm_level,
                        'resolved': False,
                        'resolved_by': None,
                        'detected_at': int(time.time()),
                        'resolved_at': None
                    }

                    insert_cols = []
                    insert_vals = {}
                    for k, v in candidate_params.items():
                        if k in cols:
                            insert_cols.append(k)
                            insert_vals[k] = v

                    # 对于表中非空且没有默认值的列，如果未提供则尝试生成合理的占位值
                    for col_name, meta in cols.items():
                        if col_name not in insert_cols and meta.get('nullable') == 'NO' and not meta.get('default'):
                            if 'id' in col_name or col_name.endswith('_id'):
                                insert_cols.append(col_name)
                                insert_vals[col_name] = int(time.time() * 1000)
                            elif col_name in ('resolved',):
                                insert_cols.append(col_name)
                                insert_vals[col_name] = False
                            else:
                                insert_cols.append(col_name)
                                insert_vals[col_name] = ''

                    if not insert_cols:
                        print("[AlarmManager] 没有可用于回退插入的列，跳过 clean_alarm 回退插入")
                    else:
                        cols_sql = ", ".join(insert_cols)
                        vals_sql = ", ".join([f":{c}" for c in insert_cols])
                        fallback_sql = f"INSERT INTO clean_alarm ({cols_sql}) VALUES ({vals_sql})"
                        print(f"[AlarmManager] 尝试回退插入 clean_alarm，使用列: {insert_cols}")
                        conn2.execute(text(fallback_sql), insert_vals)
                        print("[AlarmManager] 回退插入 clean_alarm 成功")
            except Exception as e2:
                print(f"[AlarmManager] Failed to fetch clean_alarm schema info or fallback insert failed: {e2}")

    # ========== 刷新客户端列表 ==========

    def _client_refresh_loop(self):
        """定期刷新客户端列表（从 ClientManager 同步）"""
        print("[InferenceManager] Client refresh thread started")
        while not self._stop_event.is_set():
            try:
                if self._model_worker_service:
                    self._model_worker_service.refresh_client_queues()
                time.sleep(5)  # 每 5 秒刷新一次
            except Exception as e:
                print(f"[InferenceManager] Client refresh error: {e}")

    # ========== 启动/停止 ==========

    def start(self):
        """启动推理管理器"""
        print("[InferenceManager] 启动中...")

        # 1. 启动推理服务
        if self._model_worker_service:
            self._model_worker_service.start()

        # 2. 启动异步管道（如果启用）
        if self.use_async_pipeline:
            self.temporal_pool.start()
            self.visualization_pool.start()
            self.writeback_pool.start()

        # 3. 启动持久化线程（HLS段落盘）
        if self._persist_thread is None or not self._persist_thread.is_alive():
            self._persist_thread = threading.Thread(
                target=self._persistent_worker, daemon=True, name="PersistThread"
            )
            self._persist_thread.start()

        # 3.5. 启动分段检查线程（周期性检查队列并触发落盘）
        if self._segment_check_thread is None or not self._segment_check_thread.is_alive():
            self._segment_check_thread = threading.Thread(
                target=self._segment_check_loop, daemon=True, name="SegmentCheckThread"
            )
            self._segment_check_thread.start()

        # 4. 启动告警批量flush线程
        if self._alarm_flush_thread is None or not self._alarm_flush_thread.is_alive():
            self._alarm_flush_thread = threading.Thread(
                target=self._alarm_flush_loop, daemon=True, name="AlarmFlushThread"
            )
            self._alarm_flush_thread.start()

        # 5. 启动告警持久化线程（告警上报与落库）
        if self._alarm_persist_thread is None or not self._alarm_persist_thread.is_alive():
            self._alarm_persist_thread = threading.Thread(
                target=self._alarm_persist_worker, daemon=True, name="AlarmPersistThread"
            )
            self._alarm_persist_thread.start()

        # 6. 启动客户端刷新线程
        if self._refresh_thread is None or not self._refresh_thread.is_alive():
            self._refresh_thread = threading.Thread(
                target=self._client_refresh_loop, daemon=True, name="ClientRefreshThread"
            )
            self._refresh_thread.start()

        print("[InferenceManager] 已启动")

    def stop(self):
        """停止推理管理器"""
        print("[InferenceManager] 停止中...")

        # 设置停止事件
        self._stop_event.set()

        # 1. 停止推理服务
        if self._model_worker_service:
            self._model_worker_service.stop()

        # 2. 停止异步管道（如果启用）
        if self.use_async_pipeline:
            self.temporal_pool.stop()
            self.visualization_pool.stop()
            self.writeback_pool.stop()

        # 3. 停止持久化线程（HLS段落盘）
        if self._persist_thread is not None:
            try:
                self._persist_thread.join(timeout=2.0)
            except Exception:
                pass

        # 3.5. 停止分段检查线程
        if self._segment_check_thread is not None:
            try:
                self._segment_check_thread.join(timeout=2.0)
            except Exception:
                pass

        # 4. 停止告警批量flush线程
        if self._alarm_flush_thread is not None:
            try:
                self._alarm_flush_thread.join(timeout=2.0)
            except Exception:
                pass

        # 5. 停止告警持久化线程
        if self._alarm_persist_thread is not None:
            try:
                self._alarm_persist_thread.join(timeout=2.0)
            except Exception:
                pass

        # 6. 停止客户端刷新线程
        if self._refresh_thread is not None:
            try:
                self._refresh_thread.join(timeout=2.0)
            except Exception:
                pass

        print("[InferenceManager] 已停止")

