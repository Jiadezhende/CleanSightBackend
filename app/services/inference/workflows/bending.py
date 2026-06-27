"""弯折检测：BendingDetector（流源）+ BendingOperator（流算子，analyze+judge 合一）

BendingDetector（推理线程）：
    YOLO11n-det 检测内镜先端状态（straight / bent）。
    无状态，多 Client 共享同一实例，产出 "bending" 流。

BendingOperator（时序线程，流算子）：
    订阅 "bending" 流。analyze：5 帧去抖状态机，统计 STRAIGHT→BENT 次数 bend_actions（单份）。
    judge：实时只产 events（进度 overlay），不上报告警。
    finalize：bend_actions < required → warning 结算告警。
    有状态（state/consec_*/bend_actions/last_ts），每 Client 独立。
    （合并前 bend_actions 在 Analyzer/Judge 各存一份，合并后单份共享。）
"""

import logging
from typing import Dict, List, Tuple

from app.services.inference.workflows.detector import YOLODetector
from app.services.inference.workflows.operator import Operator
from app.services.inference.data_models import (
    AlarmInfo,
    AlarmMetric,
    AlarmType,
    DetectionOutput,
    VisualizationData,
    VisItem,
    VisualizationType,
)

logger = logging.getLogger(__name__)


# ====== 推理线程：Detector ======

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

    def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
        items = []
        for det in output.detections:
            if det.class_name == "bending_debug_box":
                color = (255, 0, 255)
            elif det.class_name == "bent":
                color = (0, 0, 255)
            else:
                color = (0, 255, 0)

            items.append(VisItem(
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

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )


# ====== 时序线程：Operator（analyze 推进状态 + judge/finalize 出告警，共享 _sm）======

class BendingOperator(Operator):
    """弯折合格流算子。订阅 "bending" 流。持 debounce/required 参数。

    共享状态机（self._sm）：
        state: "STRAIGHT" | "BENT"
        consec_bent: 连续检测到 bent 的帧数
        consec_straight: 连续未检测到 bent 的帧数
        bend_actions: STRAIGHT→BENT 完成次数（累计，单份；judge/finalize 直读）
        last_ts: 游标，已处理到的最新帧 timestamp
    实时阶段 judge 只产 events（进度 overlay）不告警；terminate 时 finalize 不足则 warning。
    """

    def __init__(
        self,
        name: str = "bending",
        subscribes: List[str] = None,
        window_seconds: float = 10.0,
        debounce_frames: int = 5,
        required_bend_actions: int = 4,
    ):
        super().__init__(
            name=name,
            subscribes=subscribes or ["bending"],
            window_seconds=window_seconds,
        )
        self.debounce_frames = debounce_frames
        self.required_bend_actions = required_bend_actions
        self._sm = {
            "state": "STRAIGHT",
            "consec_bent": 0,
            "consec_straight": 0,
            "bend_actions": 0,
            "last_ts": 0.0,
        }

    def analyze(self, windows: Dict[str, List[DetectionOutput]]) -> None:
        window = self.primary_window(windows)
        if not window:
            return
        self._advance(window)

    def judge(self) -> Tuple[List[str], List[AlarmInfo]]:
        bend_actions = self._sm["bend_actions"]
        events = (
            [f"弯曲动作 {bend_actions}/{self.required_bend_actions}"]
            if bend_actions > 0 else []
        )
        return events, []  # 实时阶段不上报告警

    def finalize(self) -> List[AlarmInfo]:
        """结算：弯曲次数不足时上报 warning。"""
        bend_actions = self._sm["bend_actions"]
        if bend_actions < self.required_bend_actions:
            return [AlarmInfo(
                alarm_type=AlarmType.PROCESS_VIOLATION,
                alarm_level="warning",
                alarm_message=(
                    f"弯曲动作不足：完成 {bend_actions} 次，"
                    f"要求 {self.required_bend_actions} 次"
                ),
                metric=AlarmMetric.BENDING,
                metadata={
                    "bend_actions": bend_actions,
                    "required": self.required_bend_actions,
                },
            )]
        return []

    def _advance(self, window: List[DetectionOutput]) -> None:
        """游标推进：仅处理上次 tick 之后的新帧，逐帧驱动状态机。"""
        last_ts = self._sm["last_ts"]
        new_frames = [f for f in window if f.timestamp > last_ts]
        if not new_frames:
            return

        for frame in new_frames:
            has_bent = any(d.class_name == "bent" for d in frame.detections)

            if self._sm["state"] == "STRAIGHT":
                if has_bent:
                    self._sm["consec_bent"] += 1
                    self._sm["consec_straight"] = 0
                    if self._sm["consec_bent"] >= self.debounce_frames:
                        self._sm["state"] = "BENT"
                        self._sm["bend_actions"] += 1
                        self._sm["consec_bent"] = 0
                        logger.debug(
                            "[bending] STRAIGHT→BENT (total=%d)", self._sm["bend_actions"]
                        )
                else:
                    self._sm["consec_bent"] = 0

            elif self._sm["state"] == "BENT":
                if not has_bent:
                    self._sm["consec_straight"] += 1
                    self._sm["consec_bent"] = 0
                    if self._sm["consec_straight"] >= self.debounce_frames:
                        self._sm["state"] = "STRAIGHT"
                        self._sm["consec_straight"] = 0
                        logger.debug("[bending] BENT→STRAIGHT")
                else:
                    self._sm["consec_straight"] = 0

        self._sm["last_ts"] = new_frames[-1].timestamp
