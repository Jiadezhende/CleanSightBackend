"""离线链路落盘契约测试（本轮不实现离线推理 worker）

online/offline 已彻底分离：实时链路只产 EventFact、不落盘事实；
FeatureStore（特征）与 FactLedger（事实，offline 预置）按 (task_id, step_id) 落盘，
与 HLS 同款工作目录。本测试守住这两个落盘基座的 round-trip 契约：
- FeatureStore.append/load(task_id, step_id, ...)：常开落盘的特征可按 source 回读为 DetectionOutput 全序列
- FactLedger：EventFact / SegmentFact 混合落盘与按 type 判别回读（offline 数据契约）
"""

import tempfile

from app.services.inference.data_models import (
    Detection,
    FrameDetections,
    EventFact,
    SegmentFact,
)
from app.services.inference.models import FrameInference
from app.services.inference.store import FactLedger, FeatureStore


def _make_result(ts: float, bubble_n: int, bending_n: int) -> FrameInference:
    def dets(n, cls):
        return [
            Detection(bbox=[0, 0, 10, 10], confidence=0.9, class_id=0, class_name=cls)
            for _ in range(n)
        ]

    return FrameInference(
        client_id="c1",
        timestamp=ts,
        stage="LEAK",
        result={
            "bubble": FrameDetections(detections=dets(bubble_n, "bubble"), metadata={}, timestamp=ts),
            "bending": FrameDetections(detections=dets(bending_n, "bent"), metadata={}, timestamp=ts),
        },
    )


def test_feature_store_load_roundtrip():
    """落盘的多模型特征可按 (task, step) + source 回读为 DetectionOutput 全序列。"""
    d = tempfile.mkdtemp()
    fs = FeatureStore(d, batch_size=2)
    fs.append(7, 1, _make_result(1.0, bubble_n=2, bending_n=0))
    fs.append(7, 1, _make_result(2.0, bubble_n=0, bending_n=1))
    fs.append(7, 1, _make_result(3.0, bubble_n=3, bending_n=0))
    fs.close(7, 1)

    bubble_seq = fs.load(7, 1, source="bubble")
    assert [o.timestamp for o in bubble_seq] == [1.0, 2.0, 3.0]
    assert [len(o.detections) for o in bubble_seq] == [2, 0, 3]
    assert bubble_seq[0].detections[0].class_name == "bubble"

    bending_seq = fs.load(7, 1, source="bending")
    assert [len(o.detections) for o in bending_seq] == [0, 1, 0]


def test_feature_store_step_isolation():
    """同 task 不同 step 的特征落到独立目录，互不串台。"""
    d = tempfile.mkdtemp()
    fs = FeatureStore(d, batch_size=2)
    fs.append(7, 1, _make_result(1.0, bubble_n=2, bending_n=0))
    fs.append(7, 2, _make_result(1.0, bubble_n=5, bending_n=0))
    fs.close(7, 1)
    fs.close(7, 2)

    assert [len(o.detections) for o in fs.load(7, 1, source="bubble")] == [2]
    assert [len(o.detections) for o in fs.load(7, 2, source="bubble")] == [5]


def test_feature_store_load_missing_returns_empty():
    fs = FeatureStore(tempfile.mkdtemp())
    assert fs.load(999, 1, source="bubble") == []


def test_fact_ledger_mixed_roundtrip():
    """FactLedger 支持 EventFact / SegmentFact 混合落盘与按 type 判别回读（offline 契约）。"""
    fl = FactLedger(tempfile.mkdtemp(), batch_size=8)
    fl.append(7, 1, [
        EventFact(source="bubble", signal="birth_rate", value=0.7, ts=1.0),
        SegmentFact(source="clean", label="long_brushing", start=0.0, end=5.0),
    ])
    fl.close(7, 1)

    loaded = fl.load(7, 1)
    assert [type(f).__name__ for f in loaded] == ["EventFact", "SegmentFact"]
    assert loaded[0].signal == "birth_rate"
    assert loaded[1].label == "long_brushing"
