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
from typing import Any, Dict

from app.services.client import client_manager
from app.services.inference.components.temporal_analyzer import TemporalAnalyzer
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
        analyzer: TemporalAnalyzer,
        stop_event: threading.Event,
        worker_id: int = 0,
    ):
        """初始化时序分析工作线程。

        Args:
            input_queue: 推理结果队列
            output_queue: 可视化数据包队列
            analyzer: 时序分析器
            stop_event: 停止事件
            worker_id: 工作线程ID（用于调试）
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.analyzer = analyzer
        self.stop_event = stop_event
        self.worker_id = worker_id

    def run(self):
        """工作循环。"""
        logger.debug("[TemporalWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                # 1. 从队列获取推理结果（超时0.1秒）
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

                # 3. 执行时序分析
                temporal_result = self.analyzer.analyze(
                    state=cq.state,
                    result=result,
                    current_timestamp=result.timestamp,
                )

                # 4. 生成前端消息
                frontend_msg = self._create_frontend_message(result, temporal_result)

                # 5. 组装数据包
                data_package = TemporalAnalysisPackage(
                    client_id=result.client_id,
                    timestamp=result.timestamp,
                    stage=result.stage,
                    inference_result=result.result,
                    temporal_result=temporal_result,
                    frontend_message=frontend_msg,
                    raw_frame=(
                        result.frame
                        if result.frame is not None
                        else cq.get_latest_frame()
                    ),
                )

                # 6. 投递到可视化队列
                self.output_queue.put(data_package)

            except Exception as e:
                logger.error("[TemporalWorker-%d] Exception: %s", self.worker_id, e, exc_info=True)
                import traceback

                traceback.print_exc()

        print(f"[TemporalWorker-{self.worker_id}] 已停止")

    def _create_frontend_message(
        self,
        result: InferenceResult,
        temporal: TemporalAnalysisResult,
    ) -> FrontendMessage:
        """生成前端消息。

        Args:
            result: 推理结果
            temporal: 时序分析结果

        Returns:
            前端消息
        """
        # 提取检测结果
        detections: Dict[str, bool] = {}
        confidences: Dict[str, float] = {}

        for subtask_name, subtask_res in result.result.items():
            if isinstance(subtask_res, dict):
                detected_key = f"{subtask_name}_detected"
                detections[subtask_name] = subtask_res.get(detected_key, False)
                confidences[subtask_name] = subtask_res.get("confidence", 0.0)

        # 生成状态消息
        status_msg = self._generate_status_message(temporal)

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

    def _generate_status_message(self, temporal: TemporalAnalysisResult) -> str:
        """生成状态消息。

        Args:
            temporal: 时序分析结果

        Returns:
            状态消息字符串
        """
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
        analyzer: TemporalAnalyzer,
        num_workers: int = 2,
    ):
        """初始化时序分析线程池。

        Args:
            input_queue: 推理结果队列
            output_queue: 可视化数据包队列
            analyzer: 时序分析器
            num_workers: 工作线程数量
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.analyzer = analyzer
        self.num_workers = num_workers

        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

    def start(self):
        """启动线程池。"""
        for i in range(self.num_workers):
            worker = TemporalWorker(
                input_queue=self.input_queue,
                output_queue=self.output_queue,
                analyzer=self.analyzer,
                stop_event=self._stop_event,
                worker_id=i,
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

        logger.info("[TemporalWorkerPool] Stopped")
