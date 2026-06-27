"""弯折检测：BendingDetector + BendingAnalyzer（L3 产事实）+ BendingJudge（L4 出告警）

BendingDetector（推理线程）：
    YOLO11n-det 检测内镜先端状态（straight / bent）。
    无状态，多 Client 共享同一实例。

BendingAnalyzer（时序线程，L3）：
    5 帧去抖状态机，统计 STRAIGHT→BENT 转换次数（bend_actions）。
    只产 EventFact("bending","state",...) + EventFact("bending","count",bend_actions)，不判定。
    有状态（state/consec_*/bend_actions/last_ts），每个 Client 独立实例化。

BendingJudge（时序线程，L4）：
    持 required 次数。实时阶段只产 events（进度 overlay），不上报告警。
    任务 terminate 时：bend_actions < required → 产出 warning 结算告警。
"""

import logging
from typing import Any, Dict, List, Tuple

from app.services.inference.workflows.detector import YOLODetector
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.workflows.judge import Judge
from app.services.inference.data_models import (
    AlarmInfo,
    AlarmMetric,
    AlarmType,
    DetectionOutput,
    EventFact,
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


# ====== 时序线程 L3：TemporalAnalyzer（只产事实）======

class BendingAnalyzer(TemporalAnalyzer):
    """弯折去抖时序分析器（L3）。有状态，每个 Client 独立实例化。

    测量状态机（self._sm）：
        state: "STRAIGHT" | "BENT"
        consec_bent: 连续检测到 bent 的帧数
        consec_straight: 连续未检测到 bent 的帧数
        bend_actions: STRAIGHT→BENT 完成次数（累计）
        last_ts: 游标，已处理到的最新帧 timestamp
    产出：EventFact("bending","state",state) + EventFact("bending","count",bend_actions)。
    """

    def __init__(self, debounce_frames: int = 5, name: str = "bending"):
        super().__init__(name=name)
        self.debounce_frames = debounce_frames
        self._sm = {
            "state": "STRAIGHT",
            "consec_bent": 0,
            "consec_straight": 0,
            "bend_actions": 0,
            "last_ts": 0.0,
        }

    def trans(self, frames: List[DetectionOutput]) -> List[DetectionOutput]:
        return frames

    def infer(self, feats: List[DetectionOutput]) -> Dict[str, Any]:
        self._advance(feats)
        return {"state": self._sm["state"], "count": self._sm["bend_actions"]}

    def post_process(self, raw: Dict[str, Any], ts: float) -> List[EventFact]:
        return [
            EventFact(source=self.name, signal="state", value=raw["state"], ts=ts),
            EventFact(source=self.name, signal="count", value=raw["count"], ts=ts),
        ]

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

# ====== 时序线程 L4：Judge（消费事实出告警）======

class BendingJudge(Judge):
    """弯折合格判定（L4）。持 required 次数。

    决策状态机（self._sm）：
        bend_actions: 最近一次 tick 看到的累计弯曲次数（由 count 事实更新；结算时比较）
    实时阶段只产 events（进度 overlay），不上报告警；terminate 时不足则 warning。
    """

    def __init__(self, required_bend_actions: int = 4, name: str = "bending"):
        super().__init__(name=name)
        self.required_bend_actions = required_bend_actions
        self._sm = {"bend_actions": 0}

    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        if not facts:
            return [], []
        frame = self._frame(facts)
        cnt = frame.get("count")
        if cnt is not None:
            self._sm["bend_actions"] = cnt.value
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
