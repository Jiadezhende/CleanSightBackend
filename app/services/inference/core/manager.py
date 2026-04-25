"""推理管理器 - 核心实现

架构特点：
1. 推理与可视化解耦：推理线程只负责推理，可视化独立定时拉取
2. 时序分析独立：ClientTemporalActor 持有 TemporalAnalyzer 实例（per-client），1Hz tick
3. 三池独立时钟：推理、时序分析、可视化各自独立节奏，不通过队列串联
4. 双写 + 原子快照：推理结果同时写入 slide_window（历史）和 latest_inference（最新快照）

数据流：
InferenceLoop → cq.push_detection() + cq.set_latest_inference()  [双写]
TemporalActor (1Hz)  → cq.get_slide_window() → analyzer.analyze_temporal() → cq.set_latest_temporal()
VisualizationWorker (~15Hz) → cq.get_latest_inference() + get_latest_frame() + get_latest_temporal() → render → cq
"""

import base64
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import cv2
import numpy as np

logger = logging.getLogger(__name__)

from app.models.frame import FrameData, ProcessedFrame
from app.models.task import Task as CleaningTask
from app.services.client import ClientQueues, client_manager
from app.services.inference.core.service import ModelWorkerService
from app.services.inference.data_models import AlarmInfo
from app.services.inference.workers.temporal import ClientTemporalActor
from app.services.inference.workers.visualization import VisualizationWorkerPool


class InferenceManager:
    """推理管理器

    集成三个独立时钟的 Worker 池：
    - ModelWorkerService（推理，~30 FPS）
    - ClientTemporalActor（时序分析，1 Hz，per-client）
    - VisualizationWorkerPool（可视化，~15 FPS）

    三池通过 ClientQueues 上的原子槽位通信，不通过队列串联。
    """

    def __init__(
        self,
        rt_fps: int = 30,
        ca_segment_seconds: int = 10,
        db_dir: Optional[str] = None,
        ca_maxlen: int = 500,
    ):
        # 队列参数
        self._ca_segment_len = max(10, int(rt_fps * ca_segment_seconds))
        self._ca_maxlen = max(50, ca_maxlen)

        # 数据库存储目录
        base_dir = Path(__file__).parent.parent.parent.parent.parent.resolve()
        self._db_dir = Path(db_dir) if db_dir else base_dir / "database"
        self._db_dir.mkdir(parents=True, exist_ok=True)

        self._stop_event = threading.Event()

        # stage 配置（延迟初始化）
        self._stage_configs: Optional[Dict[str, Dict[str, Any]]] = None
        self._model_worker_service: Optional[ModelWorkerService] = None

        # per-client ClientTemporalActor 注册表
        self._actors: Dict[str, ClientTemporalActor] = {}

        self.visualization_pool = VisualizationWorkerPool(
            target_fps=15,
            stage_configs=None,
        )

        self._model_worker_service = self._create_async_model_worker_service()

        from app.services.persistence import persistence_manager as _persistence_manager
        _persistence_manager.config.storage.base_dir = str(self._db_dir)
        _persistence_manager.hls_pool.strategy.db_dir = self._db_dir
        self.persistence_manager = _persistence_manager

        self._encoded_cache: Dict[str, Dict[str, Any]] = {}
        self._encoded_cache_lock = threading.Lock()

        self._refresh_thread: Optional[threading.Thread] = None

        logger.debug("[InferenceManager] Initialization completed")

    def _get_stage_configs(self) -> Dict[str, Dict[str, Any]]:
        """延迟初始化 stage 配置。

        返回结构：
        {
            "LEAK": {
                "models": [BubbleDetector, BendingDetector],   # List[Detector]，共享
                "analyzer_specs": [(BirthRateAnalyzer, {...}), ...],  # 按 Client 实例化
                "batch_size": 4,
            }
        }
        """
        if self._stage_configs is None:
            try:
                from app.services.inference.stage_factory import StageFactory
                from app.services.inference.config import load_stage_config

                config = load_stage_config()
                factory = StageFactory(config)

                stage_configs = {}
                skipped_stages = []
                for stage_name in config.list_stages():
                    detectors = factory.create_detectors_for_stage(stage_name)
                    analyzer_specs = factory.create_analyzer_specs_for_stage(stage_name)

                    if detectors:
                        stage_configs[stage_name] = {
                            "models": detectors,
                            "analyzer_specs": analyzer_specs,
                            "batch_size": config.batch_size,
                        }
                    else:
                        skipped_stages.append(stage_name)

                if stage_configs:
                    logger.info(
                        "[InferenceManager] Loaded %d stages (active): %s",
                        len(stage_configs), list(stage_configs.keys())
                    )
                    if skipped_stages:
                        logger.info(
                            "[InferenceManager] Skipped %d stages (no detectors): %s",
                            len(skipped_stages), skipped_stages
                        )
                    self._stage_configs = stage_configs
                else:
                    raise ValueError(
                        "No valid stages found in configuration file. "
                        "Please ensure inference_config.yaml contains at least one stage with valid models."
                    )
            except Exception as e:
                logger.error("[InferenceManager] Failed to load config: %s", e, exc_info=True)
                raise RuntimeError(
                    f"Failed to load inference configuration: {e}. "
                    "Please check inference_config.yaml and ensure it is properly configured."
                ) from e

        return self._stage_configs

    def _create_async_model_worker_service(self):
        from app.services.inference.core.service import ModelWorkerService

        return ModelWorkerService(
            stage_configs=self._get_stage_configs(),
            max_batch_per_stage=8,
            use_cuda_stream=True,
            num_worker_threads=2,
        )

    # ========== 公共 API ==========

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

        task_id = cq.get_task_id()
        processed_frame = self._create_processed_frame(frame_data, task_id, client_id)

        with self._encoded_cache_lock:
            self._encoded_cache[client_id] = {
                "timestamp": frame_data.timestamp,
                "b64": processed_frame.processed_frame_b64,
                "inference_result": processed_frame.inference_result,
            }

        return processed_frame

    _STEP_TO_STAGE: Dict[str, str] = {
        "1": "LEAK",
        "2": "CLEAN",
    }

    def set_task(self, client_id: str, task: Optional[CleaningTask]) -> bool:
        """为客户端设置任务，并创建对应的 ClientTemporalActor。"""
        cq = client_manager.get_client(client_id)
        if cq is None:
            return False
        cq.set_task(task)

        if task is not None:
            stage = self._STEP_TO_STAGE.get(task.current_step, "MOCK")
            if stage == "MOCK":
                logger.warning(
                    "[InferenceManager] 未知的 current_step '%s'，路由到 MOCK stage",
                    task.current_step,
                )
            cq.set_stage(stage)

        # 停止旧 actor（任务切换时），等待线程退出并收集结算告警
        old_actor = self._actors.pop(client_id, None)
        if old_actor is not None:
            try:
                settlement = old_actor.finalize_and_stop()
                if settlement and cq:
                    self._persist_settlement_alarms(client_id, cq, settlement)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Settlement alarms on task switch failed for %s: %s",
                    client_id, e,
                )

        # 按 Client 独立实例化 TemporalAnalyzer
        stage = cq.get_stage()
        stage_cfg = self._get_stage_configs().get(stage, {})
        specs: List[Tuple[Type, Dict]] = stage_cfg.get("analyzer_specs", [])

        if specs:
            analyzers = [cls(**kwargs) for cls, kwargs in specs]
            actor = ClientTemporalActor(
                client_id=client_id,
                cq=cq,
                stage=stage,
                analyzers=analyzers,
            )
            actor.start()
            self._actors[client_id] = actor
            logger.info(
                "[InferenceManager] TemporalActor created for %s (stage=%s, analyzers=%d)",
                client_id, stage, len(analyzers),
            )
        else:
            logger.debug(
                "[InferenceManager] No analyzer specs for stage %s, skipping TemporalActor",
                stage,
            )

        return True

    def get_task(self, client_id: str) -> Optional[CleaningTask]:
        cq = client_manager.get_client(client_id)
        return cq.get_task() if cq else None

    def remove_client(self, client_id: str) -> None:
        """移除客户端的推理资源。

        1. 通过 actor.finalize_and_stop() 收集结算告警并持久化
        2. 落盘残余 HLS 段
        3. 清理编码缓存
        """
        logger.info("[InferenceManager] Removing inference resources: %s", client_id)

        cq = (
            client_manager.get_client(client_id)
            if client_manager.has_client(client_id)
            else None
        )

        # Actor 持有正确的 _sm，finalize_and_stop() 调用 analyzer.finalize()
        actor = self._actors.pop(client_id, None)
        if actor is not None:
            try:
                settlement_alarms = actor.finalize_and_stop()
                if settlement_alarms and cq:
                    self._persist_settlement_alarms(client_id, cq, settlement_alarms)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Settlement alarms failed for %s: %s", client_id, e
                )

        # Actor 停止后立即清空前端可见槽位，防止 WebSocket 读到任务结束后的残留状态。
        # cq.clear() 会在 Step3（client_manager.remove_client）再次执行，此处只做提前清零。
        if cq:
            cq.set_latest_temporal([])
            cq.set_latest_rendered(None)

        if cq:
            try:
                self._flush_all_remaining_segments(client_id, cq)
                logger.info("[InferenceManager] Segments WroteBack: %s", client_id)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Failed to write back segments: %s - %s", client_id, e
                )
        else:
            logger.warning(
                "[InferenceManager] Client not found in ClientManager, skipping writeback: %s",
                client_id,
            )

        with self._encoded_cache_lock:
            self._encoded_cache.pop(client_id, None)

        logger.info("[InferenceManager] Inference resources removed: %s", client_id)

    def status(self) -> Dict[str, Any]:
        clients = client_manager.get_all_clients()
        stats = {client_id: cq.to_status_dict() for client_id, cq in clients.items()}
        return {"clients": len(clients), "queues": stats}

    # ========== 内部辅助方法 ==========

    def _create_processed_frame(
        self, frame_data: FrameData, task_id: Optional[int], client_id: str
    ) -> ProcessedFrame:
        _, buf = cv2.imencode(".jpg", frame_data.frame)
        b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

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
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                if key in ("annotated_frame", "processed_frame", "frame"):
                    continue
                result[key] = self._make_json_serializable(value)
            return result
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]
        elif isinstance(obj, np.ndarray):
            if obj.size < 100:
                return obj.tolist()
            return None
        elif isinstance(obj, (np.integer, np.floating)):
            return obj.item()
        else:
            return obj

    def _persist_settlement_alarms(
        self, client_id: str, cq: ClientQueues, alarms: List[AlarmInfo]
    ) -> None:
        """持久化结算告警（由 actor.finalize_and_stop() 收集后调用）。"""
        from app.services.inference.models import AlarmRecord, infer_alarm_metric

        stage = cq.get_stage()
        task = cq.get_task()
        task_id = cq.get_task_id()
        step_id = int(task.current_step) if task and task.current_step else None

        for alarm in alarms:
            metric = infer_alarm_metric(
                alarm_type=alarm.alarm_type,
                alarm_message=alarm.alarm_message,
                metadata=alarm.metadata or {},
            )
            self.persistence_manager.persist_alarm({
                "task_id": task_id,
                "stage": stage,
                "step_id": step_id,
                "client_id": client_id,
                "alarm_type": alarm.alarm_type,
                "alarm_metric": metric,
                "alarm_mode": "SETTLEMENT",
                "alarm_level": alarm.alarm_level,
                "alarm_message": alarm.alarm_message,
                "detection_result": alarm.metadata if alarm.metadata else None,
            })
            cq.append_alarm_record(AlarmRecord(
                alarm_type=alarm.alarm_type,
                alarm_level=alarm.alarm_level,
                alarm_message=alarm.alarm_message,
                mode="SETTLEMENT",
                metric=metric,
                stage=stage,
                metadata=alarm.metadata or {},
            ))
            logger.info(
                "[InferenceManager] Settlement alarm for %s: %s",
                client_id, alarm.alarm_message,
            )

    def _flush_all_remaining_segments(
        self, client_id: str, client_queues: ClientQueues
    ) -> None:
        try:
            task_id = client_queues.get_task_id()
            if task_id is None:
                logger.warning(
                    "[InferenceManager] task_id is None for %s, skipping flush", client_id
                )
                return

            seg_len = client_queues.ca_segment_len
            raw_frames = client_queues.drain_ca_raw()
            processed_frames = client_queues.drain_ca_processed()

            for i in range(0, len(raw_frames), seg_len):
                chunk = raw_frames[i : i + seg_len]
                if chunk:
                    self.persistence_manager.persist_hls_segment(
                        client_id=client_id,
                        task_id=task_id,
                        segment_type="raw",
                        frames=chunk,
                    )

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
            logger.error("_flush_all_remaining_segments error for %s: %s", client_id, e, exc_info=True)

    def enqueue_alarm(self, alarm_info: Dict[str, Any]):
        self.persistence_manager.persist_alarm(alarm_info)

    # ========== 启动/停止 ==========

    def start(self):
        logger.info("[InferenceManager] 启动中...")

        if self._model_worker_service:
            self._model_worker_service.start()

        if self._model_worker_service:
            self.visualization_pool.stage_configs = self._get_stage_configs()

        self.visualization_pool.start()
        self.persistence_manager.start()

        # 初始化全局 task_name → AlarmMetric 映射（由 YAML model name 驱动）
        from app.services.inference.stage_factory import StageFactory
        from app.services.inference.config import load_stage_config
        from app.services.inference.data_models import _set_task_metric_map
        _set_task_metric_map(StageFactory(load_stage_config()).build_task_metric_map())

        logger.info("[InferenceManager] Started")

    def stop(self):
        self._stop_event.set()

        if self._model_worker_service:
            self._model_worker_service.stop()

        # 停止所有 actor，等待线程退出后再停止下游服务
        actors = list(self._actors.items())   # [(client_id, actor), ...]
        self._actors.clear()

        # Phase 1: 并行发出停止信号（非阻塞）
        for _, actor in actors:
            actor.signal_stop()

        # Phase 2: 逐个 join，收集并持久化结算告警
        for client_id, actor in actors:
            try:
                settlement = actor.finalize_and_stop()
                if settlement:
                    cq = client_manager.get_client(client_id)
                    if cq:
                        self._persist_settlement_alarms(client_id, cq, settlement)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Settlement alarms on stop failed for %s: %s",
                    client_id, e,
                )

        self.visualization_pool.stop()
        self.persistence_manager.stop(timeout=10.0)

        if self._refresh_thread is not None:
            try:
                self._refresh_thread.join(timeout=2.0)
            except Exception:
                pass

        logger.info("[InferenceManager] Stopped")
