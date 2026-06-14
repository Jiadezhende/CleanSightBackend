from typing import List, Tuple

import numpy as np

from app.services.client.queues import ClientQueues
from app.services.inference.data_models import AlarmInfo, Detection, DetectionOutput
from app.services.inference.workers.temporal import ClientTemporalActor
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.workflows import mstcn_phase


class StaticBubbleEventAnalyzer(TemporalAnalyzer):
    def __init__(self):
        super().__init__(name="bubble")

    def analyze_temporal(
        self, window: List[DetectionOutput]
    ) -> Tuple[List[str], List[AlarmInfo]]:
        return ["bubble_birth_rate=0.60 (>0.5)"], []


class FakeMSTCNRuntime:
    def __init__(self, *args, **kwargs):
        pass

    def predict(self, features: np.ndarray):
        assert features.shape == (20, 3)
        return {
            "ids": [0, 1, 1],
            "labels": ["Idle", "Long_Brushing", "Long_Brushing"],
            "confidences": [0.7, 0.8, 0.91],
            "current_id": 1,
            "current_label": "Long_Brushing",
            "confidence": 0.91,
        }


def _clean_tool_output(ts: float) -> DetectionOutput:
    return DetectionOutput(
        detections=[
            Detection(
                bbox=[20, 10, 60, 30],
                confidence=0.9,
                class_id=0,
                class_name="Hand",
            )
        ],
        metadata={"frame_shape": (100, 200, 3)},
        timestamp=ts,
    )


def test_temporal_actor_merges_bubble_and_mstcn_phase_events(monkeypatch):
    monkeypatch.setattr(mstcn_phase, "MSTCNRuntime", FakeMSTCNRuntime)

    cq = ClientQueues(client_id="client-a")
    for ts in [1.0, 2.0, 3.0]:
        cq.push_detection("clean_tool", _clean_tool_output(ts))
        cq.push_detection(
            "bubble",
            DetectionOutput(detections=[], metadata={}, timestamp=ts),
        )

    phase_analyzer = mstcn_phase.MSTCNPhaseAnalyzer(
        source_task_name="clean_tool",
        min_frames=3,
        max_frames=3,
    )
    actor = ClientTemporalActor(
        client_id="client-a",
        cq=cq,
        stage="CLEAN",
        analyzers=[StaticBubbleEventAnalyzer(), phase_analyzer],
    )

    actor._tick()

    assert cq.get_latest_temporal() == [
        "bubble_birth_rate=0.60 (>0.5)",
        "mstcn_phase=Long_Brushing conf=0.91",
    ]


def test_mstcn_phase_analyzer_owns_cursor_and_feature_window(monkeypatch):
    shapes = []

    class ShapeRecordingRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def predict(self, features: np.ndarray):
            shapes.append(features.shape)
            return {
                "ids": [1] * features.shape[1],
                "labels": ["Long_Brushing"] * features.shape[1],
                "confidences": [0.91] * features.shape[1],
                "current_id": 1,
                "current_label": "Long_Brushing",
                "confidence": 0.91,
            }

    monkeypatch.setattr(mstcn_phase, "MSTCNRuntime", ShapeRecordingRuntime)

    analyzer = mstcn_phase.MSTCNPhaseAnalyzer(min_frames=2, max_frames=2)

    events, alarms = analyzer.analyze_temporal(
        [_clean_tool_output(1.0), _clean_tool_output(2.0)]
    )
    assert events == ["mstcn_phase=Long_Brushing conf=0.91"]
    assert alarms == []
    assert shapes == [(20, 2)]
    assert analyzer._sm["last_ts"] == 2.0
    assert [item.timestamp for item in analyzer._sm["frame_buffer"]] == [1.0, 2.0]

    events, alarms = analyzer.analyze_temporal(
        [_clean_tool_output(1.0), _clean_tool_output(2.0), _clean_tool_output(3.0)]
    )
    assert events == ["mstcn_phase=Long_Brushing conf=0.91"]
    assert alarms == []
    assert shapes == [(20, 2), (20, 2)]
    assert analyzer._sm["last_ts"] == 3.0
    assert [item.timestamp for item in analyzer._sm["frame_buffer"]] == [2.0, 3.0]
