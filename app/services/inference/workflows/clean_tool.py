"""Clean tool detection for MS-TCN phase recognition.

CleanToolDetector is the spatial detector that feeds the online MS-TCN
TemporalAnalyzer.  It detects the same four classes used during MS-TCN
feature extraction:

- Hand
- Long_Brush_Head
- Scope_Port
- Short_Brush
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import numpy as np

from app.services.inference.data_models import (
    DetectionOutput,
    VisItem,
    VisualizationData,
    VisualizationType,
)
from app.services.inference.workflows.detector import YOLODetector

logger = logging.getLogger(__name__)


class CleanToolDetector(YOLODetector):
    """YOLO detector that writes clean-tool observations to slide_window."""

    EXPECTED_CLASSES = {
        "Hand",
        "Long_Brush_Head",
        "Scope_Port",
        "Short_Brush",
    }

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="clean_tool",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )


    # 这个函数在 infer() 和 infer_batch() 后被调用，负责在输出中添加一些额外的元数据字段，供后续分析器使用。
    def infer_batch(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]],
    ) -> List[DetectionOutput]:
        try:
            outputs = self._run_yolo_batch(frames)
            for output in outputs:
                self._annotate_output(output)
            return outputs
        except Exception as e:
            logger.error(
                "[CleanToolDetector] Batch inference failed, fallback: %s",
                e,
                exc_info=True,
            )
            results: List[DetectionOutput] = []
            for frame, ctx in zip(frames, contexts):
                try:
                    output = self.infer(frame, ctx)
                    self._annotate_output(output)
                    results.append(output)
                except Exception as err:
                    results.append(
                        DetectionOutput(
                            detections=[],
                            metadata={"error": str(err), "model": "clean_tool_yolo"},
                            timestamp=time.time(),
                            success=False,
                            error=str(err),
                        )
                    )
            return results

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        output = super().infer(frame, context)
        self._annotate_output(output)
        return output

    '''
        Args:
            frame: 输入图像帧
            context: 额外上下文信息（如 timestamp）
    '''
    def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
        color_map = {
            "Hand": (255, 180, 0),
            "Long_Brush_Head": (0, 220, 255),
            "Scope_Port": (180, 120, 255),
            "Short_Brush": (0, 255, 120),
        }

        items = [
            VisItem(
                bbox=det.bbox,
                label=f"{det.class_name} {det.confidence:.2f}",
                confidence=det.confidence,
                color=color_map.get(det.class_name, (255, 255, 255)),
            )
            for det in output.detections
        ]

        count = len(output.detections)
        status_text = f"Clean tools: {count}" if count else "No clean tool"
        status_color = (0, 255, 120) if count else (160, 160, 160)

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )

    def _annotate_output(self, output: DetectionOutput) -> None:
        output.success = True
        output.metadata["model"] = "clean_tool_yolo"
        output.metadata["tool_detection_count"] = len(output.detections)
        output.metadata["expected_classes"] = sorted(self.EXPECTED_CLASSES)
