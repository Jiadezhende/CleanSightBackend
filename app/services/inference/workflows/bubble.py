"""气泡检测任务（重构版）

使用新架构：
1. 检测策略（YOLOStrategy）+ 输出适配器（YOLOAdapter）
2. 时序分析逻辑内置（连续3帧检测）
3. 可视化数据准备（供固定渲染器使用）
4. 告警评估逻辑（连续3帧触发告警）
"""

import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np

from app.services.inference.workflows.infer_workflow import InferenceWorkflow
from app.services.inference.data_models import (
    AlarmInfo,
    DetectionOutput,
    TemporalResult,
    VisualizationData,
    VisItem,
    VisualizationType,
)
from app.services.inference.workflows.base import YOLODetector, YOLOAdapter
from app.utils.exceptions import ModelInferenceError

logger = logging.getLogger(__name__)


class BubbleDetectionTask(InferenceWorkflow):
    """气泡检测任务（新架构）

    时序逻辑：连续3帧检测到气泡才触发事件
    告警逻辑：触发事件时上报告警
    """

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.5,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        """初始化气泡检测任务

        Args:
            model_path: YOLO 模型路径
            conf_threshold: 置信度阈值 (0.0-1.0)
            iou_threshold: IOU 阈值 (0.0-1.0)
            enabled: 是否启用此任务
        """
        super().__init__(name="bubble", enabled=enabled)

        if not model_path:
            raise ValueError("model_path is required for BubbleDetectionTask")

        # 配置参数
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold

        # 内部组装策略和适配器
        self.strategy = YOLODetector()
        self.adapter = YOLOAdapter()

        # 延迟加载模型
        self._model_loaded = False

    def _ensure_model_loaded(self):
        """确保模型已加载"""
        if not self._model_loaded:
            try:
                self.strategy.load_model(self.model_path)
                self._model_loaded = True
                logger.info(f"[BubbleTask] Model loaded: {self.model_path}")
            except Exception as e:
                logger.error(f"[BubbleTask] Model loading failed: {e}", exc_info=True)
                raise ModelInferenceError(
                    message=f"Failed to load bubble detection model: {str(e)}",
                    model_name="yolov8_bubble",
                ) from e

    # ====== 1. 检测 ======

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        """执行气泡检测

        Args:
            frame: 输入图像
            context: 上下文（包含 task、client_id 等）

        Returns:
            DetectionOutput: 标准化检测输出
        """
        try:
            # 确保模型已加载
            self._ensure_model_loaded()

            # 执行检测
            raw_output = self.strategy.detect(
                frame, conf=self.conf_threshold, iou=self.iou_threshold
            )

            # 适配输出
            detection_output = self.adapter.adapt(raw_output, frame, time.time())

            return detection_output

        except RuntimeError as e:
            # YOLO RuntimeError → ModelInferenceError
            error_msg = str(e).lower()
            is_cuda = "out of memory" in error_msg or "cuda" in error_msg

            raise ModelInferenceError(
                message=str(e),
                model_name="yolov8_bubble",
                client_id=context.get("client_id"),
                is_cuda_error=is_cuda,
            ) from e

        except Exception as e:
            raise ModelInferenceError(
                message=f"Unexpected error in bubble detection: {str(e)}",
                model_name="yolov8_bubble",
                client_id=context.get("client_id"),
            ) from e

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        """批量气泡检测

        利用 YOLO 的批量推理接口提升性能

        Returns:
            DetectionOutput 列表
        """
        try:
            self._ensure_model_loaded()

            # 批量检测
            raw_outputs = self.strategy.detect_batch(
                frames, conf=self.conf_threshold, iou=self.iou_threshold
            )

            # 批量适配
            timestamp = time.time()
            detection_outputs = [
                self.adapter.adapt(out, frame, timestamp)
                for out, frame in zip(raw_outputs, frames)
            ]

            # 设置业务字段（直接修改 DetectionOutput 对象）
            for output in detection_outputs:
                output.success = True
                output.bubble_detected = len(output.detections) > 0
                output.bubble_count = len(output.detections)

            return detection_outputs

        except Exception as e:
            logger.error(f"[BubbleTask] Batch inference failed: {e}, fallback to single", exc_info=True)
            # 降级到单帧处理
            results = []
            for f, c in zip(frames, contexts):
                try:
                    output = self.infer(f, c)
                    results.append({
                        "detection_output": output,
                        "bubble_detected": len(output.detections) > 0,
                        "bubble_count": len(output.detections),
                        "success": True,
                    })
                except Exception as err:
                    results.append({"success": False, "error": str(err)})
            return results

    # ====== 2. 时序分析 ======

    def analyze_temporal(
        self, state, output: DetectionOutput, timestamp: float
    ) -> TemporalResult:
        """气泡时序分析：连续3帧检测到才触发事件

        Args:
            state: ClientState 实例
            output: 检测输出
            timestamp: 时间戳

        Returns:
            TemporalResult: 时序分析结果
        """
        # 检测结果
        bubble_count = len(output.detections)
        detected = bubble_count > 0

        # 更新连续检测计数
        if detected:
            consecutive = state.increment_counter("bubble_consecutive")
        else:
            state.reset_counter("bubble_consecutive")
            consecutive = 0

        # 累计总数
        if detected:
            total = state.increment_counter("bubble_total", delta=bubble_count)
        else:
            total = state.get_counter("bubble_total", 0)

        # 事件触发：连续3帧
        event_triggered = consecutive >= 3
        event_message = (
            f"连续{consecutive}帧检测到气泡" if event_triggered else None
        )

        return TemporalResult(
            detected=detected,
            event_triggered=event_triggered,
            event_message=event_message,
            counters={
                "bubble_count": bubble_count,
                "consecutive_frames": consecutive,
                "total_bubbles": total,
            },
        )

    # ====== 3. 可视化数据准备 ======

    def prepare_visualization_data(
        self, output: DetectionOutput, temporal: TemporalResult
    ) -> VisualizationData:
        """准备气泡可视化数据

        Args:
            output: 检测输出
            temporal: 时序分析结果

        Returns:
            VisualizationData: 可视化数据
        """
        # 准备检测框可视化项
        items = []
        for det in output.detections:
            # 颜色：检测到气泡用黄色，调试框用洋红色
            if det.class_name == "bubble_debug_box":
                color = (255, 0, 255)  # 洋红色（调试框）
            else:
                color = (0, 255, 255)  # 黄色（正常气泡）

            items.append(
                VisItem(
                    bbox=det.bbox,
                    label=f"{det.class_name} {det.confidence:.2f}",
                    confidence=det.confidence,
                    color=color,
                )
            )

        # 状态栏
        bubble_count = temporal.counters.get("bubble_count", 0)
        total = temporal.counters.get("total_bubbles", 0)

        if temporal.detected:
            status_text = f"Bubbles: {bubble_count} (Total: {total})"
            # 气泡数超过5个时用橙色警告
            status_color = (0, 165, 255) if bubble_count > 5 else (0, 255, 255)
        else:
            status_text = f"No Bubbles (Total: {total})"
            status_color = (0, 255, 0)  # 绿色

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-right",
        )

    # ====== 4. 告警评估 ======

    def evaluate_alarms(
        self, temporal: TemporalResult, context: Dict[str, Any]
    ) -> List[AlarmInfo]:
        """评估气泡告警

        触发条件：连续3帧检测到气泡
        """
        alarms = []

        if temporal.event_triggered:
            alarms.append(
                AlarmInfo(
                    alarm_type="流程违规",
                    alarm_level="high",
                    alarm_message="检测到气泡异常（连续3帧）",
                    metadata={
                        "consecutive_frames": temporal.counters.get("consecutive_frames", 0),
                        "bubble_count": temporal.counters.get("bubble_count", 0),
                    },
                )
            )

        return alarms

    # ====== 辅助方法 ======

    def set_thresholds(self, conf_threshold: Optional[float] = None, iou_threshold: Optional[float] = None):
        """动态调整检测阈值"""
        if conf_threshold is not None:
            self.conf_threshold = max(0.0, min(1.0, conf_threshold))
        if iou_threshold is not None:
            self.iou_threshold = max(0.0, min(1.0, iou_threshold))

        logger.info(
            f"[BubbleTask] Thresholds updated: conf={self.conf_threshold}, iou={self.iou_threshold}"
        )
