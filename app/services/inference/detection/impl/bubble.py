"""气泡检测：BubbleDetector（流源，YOLO11n-seg 检测气泡实例）。

无状态，多 Client 共享同一实例，产出 "bubble" 流。
同业务点的时序算子（ByteTrack 出生率漏气告警）见 temporal/impl/bubble.py。
"""

from app.services.inference.detection.detector import YOLODetector
from app.domain.detection import FrameDetections
from app.domain.render import RenderItem, RenderSpec, RenderType


class BubbleDetector(YOLODetector):
    """气泡检测器（YOLO11n-seg）。无状态，多 Client 共享。"""

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.5,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="bubble",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )

    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        items = []
        for det in output.detections:
            color = (255, 0, 255) if det.class_name == "bubble_debug_box" else (0, 255, 255)
            items.append(RenderItem(
                bbox=det.bbox,
                label=f"{det.class_name} {det.confidence:.2f}",
                confidence=det.confidence,
                color=color,
            ))

        count = len(output.detections)
        if count > 0:
            status_text = f"Bubbles: {count}"
            status_color = (0, 165, 255) if count > 5 else (0, 255, 255)
        else:
            status_text = "No Bubbles"
            status_color = (0, 255, 0)

        return RenderSpec(
            type=RenderType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-right",
        )
