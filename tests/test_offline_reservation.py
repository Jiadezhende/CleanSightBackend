"""Phase 2 离线链路接口预留测试

验证离线链路所需接口齐备（本轮不实现离线推理 worker）：
- FeatureStore.load(task_id, source)：把常开落盘的特征回读为 DetectionOutput 全序列
- TemporalAnalyzer.load(task_id)：经 attach_feature_store() 后回读本 source 全序列
- post_process(online=False)：离线分段产出为预留接口（尚未实现 → NotImplementedError）
- FactLedger：EventFact / SegmentFact 混合 round-trip
"""

import tempfile

import pytest

from app.services.inference.data_models import (
    Detection,
    DetectionOutput,
    EventFact,
    SegmentFact,
)
from app.services.inference.models import InferenceResult
from app.services.inference.store import FactLedger, FeatureStore


def _make_result(ts: float, bubble_n: int, bending_n: int) -> InferenceResult:
    def dets(n, cls):
        return [
            Detection(bbox=[0, 0, 10, 10], confidence=0.9, class_id=0, class_name=cls)
            for _ in range(n)
        ]

    return InferenceResult(
        client_id="c1",
        timestamp=ts,
        stage="LEAK",
        result={
            "bubble": DetectionOutput(detections=dets(bubble_n, "bubble"), metadata={}, timestamp=ts),
            "bending": DetectionOutput(detections=dets(bending_n, "bent"), metadata={}, timestamp=ts),
        },
    )


def test_feature_store_load_roundtrip():
    """落盘的多模型特征可按 source 回读为 DetectionOutput 全序列。"""
    d = tempfile.mkdtemp()
    fs = FeatureStore(d, batch_size=2)
    fs.append(7, _make_result(1.0, bubble_n=2, bending_n=0))
    fs.append(7, _make_result(2.0, bubble_n=0, bending_n=1))
    fs.append(7, _make_result(3.0, bubble_n=3, bending_n=0))
    fs.close(7)

    bubble_seq = fs.load(7, source="bubble")
    assert [o.timestamp for o in bubble_seq] == [1.0, 2.0, 3.0]
    assert [len(o.detections) for o in bubble_seq] == [2, 0, 3]
    assert bubble_seq[0].detections[0].class_name == "bubble"

    bending_seq = fs.load(7, source="bending")
    assert [len(o.detections) for o in bending_seq] == [0, 1, 0]


def test_feature_store_load_missing_returns_empty():
    fs = FeatureStore(tempfile.mkdtemp())
    assert fs.load(999, source="bubble") == []


def test_analyzer_load_via_attached_store():
    """TemporalAnalyzer.load() 经 attach_feature_store 后回读本 source 全序列。"""
    from app.services.inference.workflows.bubble import BubbleAnalyzer

    d = tempfile.mkdtemp()
    fs = FeatureStore(d, batch_size=4)
    fs.append(7, _make_result(1.0, bubble_n=1, bending_n=0))
    fs.append(7, _make_result(2.0, bubble_n=2, bending_n=0))
    fs.close(7)

    analyzer = BubbleAnalyzer(window_seconds=3.0)  # name="bubble"
    analyzer.attach_feature_store(fs)
    seq = analyzer.load(7)
    assert [o.timestamp for o in seq] == [1.0, 2.0]
    assert [len(o.detections) for o in seq] == [1, 2]


def test_analyzer_load_without_store_raises():
    """未 attach FeatureStore 时 load() 视为未实现（实时链路不应走到这里）。"""
    from app.services.inference.workflows.bubble import BubbleAnalyzer

    with pytest.raises(NotImplementedError):
        BubbleAnalyzer().load(7)


@pytest.mark.parametrize("module_cls", [
    ("app.services.inference.workflows.bubble", "BubbleAnalyzer"),
    ("app.services.inference.workflows.bending", "BendingAnalyzer"),
    ("app.services.inference.workflows.mock", "MockAnalyzer"),
])
def test_offline_post_process_reserved(module_cls):
    """离线分段产出（online=False）为预留接口，尚未实现 → NotImplementedError。"""
    import importlib

    mod_name, cls_name = module_cls
    cls = getattr(importlib.import_module(mod_name), cls_name)
    analyzer = cls()
    with pytest.raises(NotImplementedError):
        # raw 形态各异，但 online=False 分支应在任何 raw 上直接抛出
        analyzer.post_process({"state": "STRAIGHT", "count": 0}, ts=1.0, online=False)


def test_fact_ledger_mixed_roundtrip():
    """FactLedger 支持 EventFact / SegmentFact 混合落盘与按 type 判别回读。"""
    fl = FactLedger(tempfile.mkdtemp(), batch_size=8)
    fl.append(7, [
        EventFact(source="bubble", signal="birth_rate", value=0.7, ts=1.0),
        SegmentFact(source="clean", label="long_brushing", start=0.0, end=5.0),
    ])
    fl.close(7)

    loaded = fl.load(7)
    assert [type(f).__name__ for f in loaded] == ["EventFact", "SegmentFact"]
    assert loaded[0].signal == "birth_rate"
    assert loaded[1].label == "long_brushing"
