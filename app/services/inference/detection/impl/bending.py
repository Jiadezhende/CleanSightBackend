"""弯折检测：BendingDetector（流源，YOLO11n-det 检测内镜先端状态 straight / bent）。

无状态，多 Client 共享同一实例，产出 "bending" 流。
同业务点的时序算子（5 帧去抖 + 结算告警）见 temporal/impl/bending.py。
"""

from app.services.inference.detection.detector import YOLODetector
from app.domain.detection import FrameDetections
from app.domain.render import RenderItem, RenderSpec, RenderType


class BendingDetector(YOLODetector):
    """内镜弯折检测器（YOLO11n-det）。无状态，多 Client 共享。"""

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.6,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="bending",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )

    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        items = []
        for det in output.detections:
            if det.class_name == "bending_debug_box":
                color = (255, 0, 255)
            elif det.class_name == "bent":
                color = (0, 0, 255)
            else:
                color = (0, 255, 0)

            items.append(RenderItem(
                bbox=det.bbox,
                label=f"{det.class_name} {det.confidence:.2f}",
                confidence=det.confidence,
                color=color,
            ))

        is_bent = any(d.class_name == "bent" for d in output.detections)
        if is_bent:
            status_text = "BENT"
            status_color = (0, 0, 255)
        else:
            status_text = "STRAIGHT"
            status_color = (0, 255, 0)

        return RenderSpec(
            type=RenderType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )
