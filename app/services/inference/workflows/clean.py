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

from typing import Dict, List, Tuple

import torch

from app.services.client.config import get_client_config
from app.services.inference.detection.detector import YOLODetector
from app.services.inference.temporal.operator import AlignedFrame, GRUOperator
from app.domain.alarm import Alarm
from app.domain.detection import FrameDetections
from app.domain.render import RenderItem, RenderSpec, RenderType

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

class CleanOperator(GRUOperator):
    def __init__(
        self,
        name: str,
        subscribes: List[str],
        window_seconds: float,
        model_path: str,
        actions: Dict[int, str],
        objects: Dict[int, str],
    ) -> None:
        super().__init__(
            name=name,
            subscribes=subscribes,
            window_seconds=window_seconds,
            model_path=model_path,
            actions=actions,
            objects=objects,
        )
        self.history_frames = []
        self._sm = {
            "last_ts": 0.0,
            "latest_action": None,
        }

    def analyze(self, windows: Dict[str, List[FrameDetections]]) -> None:
        """消费订阅流、推进 self._sm。windows: {流名: 该流滑窗快照(按 ts 升序)}。"""
        aligned_frames = self._zip_by_ts(windows)
        if not aligned_frames:
            return        
        self._advance(aligned_frames)

    def judge(self) -> Tuple[List[str], List[Alarm]]:
        """读 self._sm 出 (overlay 文案, 实时告警)。"""
        events = (
            [f"Action: {self._action_name(self._sm['latest_action'])}"]
        )
        alarms = []
        return events, alarms

    def _advance(self, aligned_frames: List[AlignedFrame]) -> None:
        """推进 self._sm。aligned_frames: 对齐后的窗口快照列表。
        逐帧滑动窗口推理
        """
        window_size = len(aligned_frames)

        last_ts = self._sm["last_ts"]
        new_frames = [f for f in aligned_frames if f.ts > last_ts]
        if not new_frames:
            return

        self.history_frames.extend(new_frames)

        if len(self.history_frames) < window_size:
            self._sm["last_ts"] = new_frames[-1].ts
            return

        num_new = len(new_frames)
        start_idx = max(0, len(self.history_frames) - num_new - window_size + 1)

        for i in range(num_new):
            window_start = start_idx + i
            window_end = window_start + window_size
            if window_end > len(self.history_frames):
                break

            window = self.history_frames[window_start:window_end]
            features = self._adapt_to_features(window)

            if features.numel() == 0:
                continue

            predictions = self.infer(features)
            self._sm["latest_action"] = predictions[-1]

        self.history_frames = self.history_frames[-window_size:]
        self._sm["last_ts"] = new_frames[-1].ts

    def _adapt_to_features(self, aligned_frames: List[AlignedFrame]) -> torch.Tensor:
        """将窗口快照转换为特征矩阵。

        修正：先按 timestamp 对齐多流，再合并同一时刻的特征。
        同一原始帧的多流检测结果合并到同一个 feature vector 中。

        特征格式：(T, input_dim)，其中 input_dim = num_objects * 4
        每个物体的特征向量为 (cx, cy, w, h) 归一化到 [0, 1] 范围。
        """

        if not aligned_frames:
            return torch.tensor([])
        
        features = []
        config = get_client_config()
        height = config.frame.resize_height
        width = config.frame.resize_width

        for aligned in aligned_frames:
            feature = [0.0] * (self.num_objects * 4)

            class_features = {}
            for stream_name, frame_detections in aligned.by_source.items():
                for detection in frame_detections.detections:
                    object_id = self._object_id(detection.class_name)
                    class_features.setdefault(object_id, []).append(detection)

            for class_id, detections in class_features.items():
                if class_id >= self.num_objects:
                    continue
                best_detection = max(detections, key=lambda x: x.confidence)
                x1, y1, x2, y2 = best_detection.bbox

                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                w, h = x2 - x1, y2 - y1

                cx, cy = cx / width, cy / height
                w, h = w / width, h / height

                base = class_id * 4
                feature[base:base+4] = cx, cy, w, h

            features.append(feature)

        return torch.tensor(features, dtype=torch.float32)
