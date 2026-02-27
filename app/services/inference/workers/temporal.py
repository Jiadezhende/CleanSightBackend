"""temporal.py - 时序分析工作线程池。

职责：
- 从推理队列消费推理结果
- 执行时序分析逻辑
- 更新 ClientState
- 生成前端消息
- 投递到可视化队列
"""

import logging
import threading
from queue import Empty, Queue
from typing import Any, Dict, Optional

import numpy as np

from app.services.client import client_manager
from app.services.inference.data_models import DetectionOutput
from app.services.inference.models import (
    FrontendMessage,
    InferenceResult,
    TemporalAnalysisPackage,
    TemporalAnalysisResult,
)

logger = logging.getLogger(__name__)


class TemporalWorker:
    """时序分析工作线程。"""

    def __init__(
        self,
        input_queue: Queue,  # 输入：推理结果
        output_queue: Queue,  # 输出：时序分析后的数据包
        stop_event: threading.Event,
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化时序分析工作线程。

        Args:
            input_queue: 推理结果队列
            output_queue: 可视化数据包队列
            stop_event: 停止事件
            worker_id: 工作线程ID（用于调试）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}

    def run(self):
        """工作循环。"""
        logger.debug("[TemporalWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                # 1. 从队列获取推理结果（超时0.1秒）
                # result.result 的类型：Dict[str, DetectionOutput]
                #   其中每个 DetectionOutput 包含检测结果和业务字段
                try:
                    result: InferenceResult = self.input_queue.get(timeout=0.1)
                except Empty:
                    continue

                # 2. 获取客户端状态
                if not client_manager.has_client(result.client_id):
                    continue

                cq = client_manager.get_client(result.client_id)
                if cq is None:
                    continue

                # 3. 调用每个 Task 的 analyze_temporal() 和 evaluate_alarms()
                temporal_result, all_alarms = self._process_with_tasks(result, cq.state)

                if temporal_result is None:
                    # stage_configs 尚未加载（启动前）或该 stage 无 tasks，生成空结果
                    temporal_result = TemporalAnalysisResult(
                        client_id=result.client_id,
                        timestamp=result.timestamp,
                        stage_changed=False,
                        new_stage=None,
                        step_completed=False,
                        events=[],
                        state_snapshot={},
                    )
                    all_alarms = []

                # 4. 生成前端消息
                frontend_msg = self._create_frontend_message(result, temporal_result, all_alarms)

                # 5. 组装数据包
                frame_to_use = result.frame if result.frame is not None else cq.get_latest_frame()
                
                data_package = TemporalAnalysisPackage(
                    client_id=result.client_id,
                    timestamp=result.timestamp,
                    stage=result.stage,
                    inference_result=result.result,
                    temporal_result=temporal_result,
                    frontend_message=frontend_msg,
                    raw_frame=frame_to_use if frame_to_use is not None else np.zeros((480, 640, 3), dtype=np.uint8),
                )

                # 6. 投递到可视化队列
                self.output_queue.put(data_package)

            except Exception as e:
                logger.error("[TemporalWorker-%d] Exception: %s", self.worker_id, e, exc_info=True)
                import traceback

                traceback.print_exc()

        logger.debug(f"[TemporalWorker-{self.worker_id}] 已停止")

    def _process_with_tasks(self, result: InferenceResult, state) -> tuple:
        """使用新架构处理时序分析
        
        Args:
            result: 推理结果
            state: 客户端状态
            
        Returns:
            (TemporalAnalysisResult, List[AlarmInfo]) 或 (None, []) 如果无法使用新架构
        """
        try:
            # 获取当前 stage 的 tasks
            stage_cfg = self.stage_configs.get(result.stage, {})
            tasks = stage_cfg.get("models", [])
            
            if not tasks:
                return None, []  # 无 tasks，回退到旧逻辑
            
            # 导入 TemporalResult 和 AlarmInfo（避免循环导入）
            from app.services.inference.data_models import TemporalResult, AlarmInfo
            
            all_events = []
            all_alarms = []
            step_completed = False
            
            # 处理每个 task
            for task in tasks:
                task_result = result.result.get(task.name)
                
                # 检查是否有 task 结果（现在是 DetectionOutput 对象）
                if task_result is None:
                    continue  # 跳过没有结果的 task
                
                # task_result 现在是 DetectionOutput 对象
                detection_output = task_result
                
                # 调用 Task 的 analyze_temporal()
                temporal_res: TemporalResult = task.analyze_temporal(
                    state, detection_output, result.timestamp
                )
                
                # 收集事件
                if temporal_res.event_message:
                    all_events.append(temporal_res.event_message)
                
                # 调用 Task 的 evaluate_alarms()
                context = {
                    "client_id": result.client_id,
                    "stage": result.stage,
                    "task_name": task.name,
                }
                alarms = task.evaluate_alarms(temporal_res, context)
                all_alarms.extend(alarms)
                
                # 检查步骤完成
                if temporal_res.event_triggered:
                    step_completed = True
            
            # 构造 TemporalAnalysisResult（此处是整合了各个task的时序分析结果向后兼容）
            temporal_result = TemporalAnalysisResult(
                client_id=result.client_id,
                timestamp=result.timestamp,
                stage_changed=False,
                new_stage=None,
                step_completed=step_completed,
                events=all_events,
                state_snapshot={},  # 简化处理
            )
            
            return temporal_result, all_alarms
            
        except Exception as e:
            logger.error(f"[TemporalWorker] Failed to process with tasks: {e}", exc_info=True)
            return None, []  # 回退到旧逻辑

    def _create_frontend_message(
        self,
        result: InferenceResult,
        temporal: TemporalAnalysisResult,
        alarms: Optional[list] = None,
    ) -> FrontendMessage:
        """生成前端消息。

        Args:
            result: 推理结果
            temporal: 时序分析结果
            alarms: 告警列表（新架构）

        Returns:
            前端消息
        """
        # 提取检测结果
        detections: Dict[str, bool] = {}
        confidences: Dict[str, float] = {}

        for subtask_name, subtask_res in result.result.items():
            if isinstance(subtask_res, DetectionOutput):
                detections[subtask_name] = len(subtask_res.detections) > 0
                if subtask_res.detections:
                    confidences[subtask_name] = sum(
                        d.confidence for d in subtask_res.detections
                    ) / len(subtask_res.detections)
                else:
                    confidences[subtask_name] = 0.0

        # 生成状态消息（包含告警信息）
        status_msg = self._generate_status_message(temporal, alarms or [])

        return FrontendMessage(
            client_id=result.client_id,
            timestamp=result.timestamp,
            stage=result.stage,
            detections=detections,
            confidences=confidences,
            status_message=status_msg,
            progress={
                "current_step": result.stage,
                "completed": temporal.step_completed,
                "events": temporal.events,
            },
        )

    def _generate_status_message(self, temporal: TemporalAnalysisResult, alarms: list) -> str:
        """生成状态消息。

        Args:
            temporal: 时序分析结果
            alarms: 告警列表

        Returns:
            状态消息字符串
        """
        # 优先显示告警
        if alarms:
            alarm_msgs = [alarm.alarm_message for alarm in alarms]
            return "⚠️ " + "; ".join(alarm_msgs)
        
        # 显示事件
        if temporal.events:
            # 有事件，显示最近的事件
            return "; ".join(temporal.events)
        elif temporal.step_completed:
            return "步骤已完成"
        else:
            return "正在检测..."


class TemporalWorkerPool:
    """时序分析线程池。"""

    def __init__(
        self,
        input_queue: Queue,
        output_queue: Queue,
        num_workers: int = 2,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化时序分析线程池。

        Args:
            input_queue: 推理结果队列
            output_queue: 可视化数据包队列
            num_workers: 工作线程数量
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.num_workers = num_workers
        self.stage_configs = stage_configs or {}

        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

    def start(self):
        """启动线程池。"""
        for i in range(self.num_workers):
            worker = TemporalWorker(
                input_queue=self.input_queue,
                output_queue=self.output_queue,
                stop_event=self._stop_event,
                worker_id=i,
                stage_configs=self.stage_configs,
            )

            thread = threading.Thread(
                target=worker.run,
                daemon=True,
                name=f"TemporalWorker-{i}",
            )
            thread.start()
            self._workers.append(thread)

        logger.info("[TemporalWorkerPool] Started %d workers", self.num_workers)

    def stop(self):
        """停止线程池。"""
        self._stop_event.set()

        for thread in self._workers:
            thread.join(timeout=2.0)

        logger.debug("[TemporalWorkerPool] Stopped")
