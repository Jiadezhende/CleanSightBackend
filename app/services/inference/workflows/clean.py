"""清洗阶段目标检测：CleanLargeDetector / CleanSmallDetector（仅检测 + 画框）

按尺寸分两组权重各一个 Detector：
    CleanLargeDetector —— clean-large-best.pt（手 / scope_control_body / scope_mid_section）
    CleanSmallDetector —— clean-small-best.pt（syringe / air_gun / scope_distal_end）

只在画面叠检测框，不产时序事实、不上报告警——CLEAN stage 的 rules 为空，
不建 Operator/Actor，仅由 detector 的 prepare_visualization_data 提供检测框可视化。

detector.name = 该 detector 产出的流名（决定 slide_window key 与 Operator.subscribes 订阅），
故各类硬编码 name（同 BubbleDetector/BendingDetector 的写法）。
"""

from __future__ import annotations

from app.services.inference.workflows.detector import YOLODetector
from app.services.inference.data_models import (
    FrameDetections,
    RenderSpec,
    RenderItem,
    RenderType,
)

# 固定调色板，按 class_id 取色（BGR）
_PALETTE = [
    (0, 255, 0),    # 绿
    (0, 255, 255),  # 黄
    (255, 128, 0),  # 蓝橙
    (255, 0, 255),  # 品红
    (0, 165, 255),  # 橙
    (255, 255, 0),  # 青
]


def _bbox_items(output: FrameDetections):
    return [
        RenderItem(
            bbox=det.bbox,
            label=f"{det.class_name} {det.confidence:.2f}",
            confidence=det.confidence,
            color=_PALETTE[det.class_id % len(_PALETTE)],
        )
        for det in output.detections
    ]


class CleanLargeDetector(YOLODetector):
    """大目标组检测器（手 / 内镜主体结构）。仅检测 + 画框。"""

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="clean_large",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )

    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        items = _bbox_items(output)
        return RenderSpec(
            type=RenderType.BBOX,
            items=items,
            status_text=f"Large: {len(items)}" if items else "Large: -",
            status_color=(0, 255, 0),
            status_position="top-right",
        )


class CleanSmallDetector(YOLODetector):
    """小目标组检测器（注射器 / 气枪 / 先端部）。仅检测 + 画框。"""

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="clean_small",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )

    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        items = _bbox_items(output)
        return RenderSpec(
            type=RenderType.BBOX,
            items=items,
            status_text=f"Small: {len(items)}" if items else "Small: -",
            status_color=(0, 255, 255),
            status_position="top-left",
        )
