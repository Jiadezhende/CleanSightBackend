"""流处理框架新增能力测试：subscribes 注入 / 感受野 _clip / 多流 zip / 缓冲按感受野 / per-operator 隔离。"""

import threading
from typing import Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from app.services.client.queues import ClientQueues
from app.services.inference.config import load_stage_config
from app.domain.alarm import Alarm, AlarmType
from app.domain.detection import Detection, FrameDetections
from app.services.inference.stage_factory import StageFactory
from app.services.inference.temporal.operator import Operator


def _out(ts: float, n: int = 1) -> FrameDetections:
    dets = [Detection(bbox=[0, 0, 1, 1], confidence=0.9, class_id=0, class_name="x") for _ in range(n)]
    return FrameDetections(detections=dets, metadata={}, timestamp=ts)


class _NoopOperator(Operator):
    def analyze(self, windows):  # noqa: D401
        self._sm["last"] = windows

    def judge(self):
        return [], []


# ========== 工厂：subscribes 注入 + metric_map key = detector name ==========


def test_factory_injects_subscribes_and_metric_keys():
    cfg = load_stage_config(force_reload=True)
    f = StageFactory(cfg)

    specs = f.create_operators_for_stage("1")
    kwargs_by_name = {kw["name"]: kw for _, kw in specs}
    assert kwargs_by_name["bubble_leak"]["subscribes"] == ["bubble"]
    assert kwargs_by_name["bending_check"]["subscribes"] == ["bending"]

    # metric_map 的 key 必须是 detector(流) 名，而非 rule 名
    metric_map = f.build_task_metric_map()
    detector_names = {
        d.get("name")
        for stage in cfg.stages.values()
        for d in stage.detectors
    }
    assert set(metric_map.keys()) <= detector_names
    assert "bubble" in metric_map           # realtime 规则订阅的流
    assert "bending" not in metric_map      # realtime:false → 不纳入
    assert "bubble_leak" not in metric_map  # 不是流名


# ========== 感受野 _clip ==========


def test_clip_to_receptive_field():
    win = [_out(t) for t in [0.0, 1.0, 2.0, 3.0, 4.0]]  # latest=4.0
    op_small = _NoopOperator(name="a", subscribes=["s"], window_seconds=2.0)
    op_big = _NoopOperator(name="b", subscribes=["s"], window_seconds=5.0)
    # 感受野 2s → 保留 ts>=2.0：2,3,4
    assert [d.timestamp for d in op_small._clip(win)] == [2.0, 3.0, 4.0]
    # 感受野 5s → 全保留
    assert [d.timestamp for d in op_big._clip(win)] == [0.0, 1.0, 2.0, 3.0, 4.0]


# ========== 多流按 ts zip（inner-join）==========


def test_zip_by_ts_inner_join():
    op = _NoopOperator(name="m", subscribes=["a", "b"], window_seconds=10.0)
    windows = {
        "a": [_out(1.0), _out(2.0), _out(3.0)],
        "b": [_out(2.0), _out(3.0), _out(4.0)],  # 缺 1.0、多 4.0
    }
    aligned = op._zip_by_ts(windows)
    assert [fr.ts for fr in aligned] == [2.0, 3.0]  # 仅共有 ts
    assert set(aligned[0].by_source.keys()) == {"a", "b"}


def test_zip_by_ts_empty_when_a_stream_missing():
    op = _NoopOperator(name="m", subscribes=["a", "b"], window_seconds=10.0)
    assert op._zip_by_ts({"a": [_out(1.0)], "b": []}) == []


# ========== subscribes 必填 ==========


def test_subscribes_required():
    with pytest.raises(ValueError):
        _NoopOperator(name="x", subscribes=[], window_seconds=3.0)


# ========== 缓冲按感受野（max(底线, 感受野)）==========


def test_stream_buffer_floor_10s():
    cq = ClientQueues()
    for t in [0.0, 5.0, 10.0, 15.0, 20.0]:
        cq.push_detection("x", _out(t))
    # 未配感受野 → 底线 10s：cutoff=20-10=10 → 保留 10,15,20
    win = cq.get_slide_window("x")
    assert [d.timestamp for d in win] == [10.0, 15.0, 20.0]


def test_stream_buffer_extends_with_receptive_field():
    cq = ClientQueues()
    cq.set_stream_windows({"x": 30.0})  # 感受野 30s > 底线
    for t in [0.0, 5.0, 10.0, 15.0, 20.0]:
        cq.push_detection("x", _out(t))
    # retain=max(10,30)=30：cutoff=20-30=-10 → 全保留
    win = cq.get_slide_window("x")
    assert [d.timestamp for d in win] == [0.0, 5.0, 10.0, 15.0, 20.0]


# ========== per-operator 异常隔离 ==========


class _BadOperator(Operator):
    def analyze(self, windows):
        raise RuntimeError("boom")

    def judge(self):
        return ["bad"], []


class _GoodOperator(Operator):
    def analyze(self, windows):
        self._sm["ran"] = True

    def judge(self):
        return ["good"], [Alarm(alarm_type=AlarmType.MOCK, alarm_level="low", alarm_message="ok")]


def test_per_operator_isolation():
    from app.services.inference.temporal.actor import ClientTemporalActor

    cq = MagicMock()
    cq.get_slide_window.return_value = [_out(1.0)]
    captured: List[Alarm] = []

    bad = _BadOperator(name="bad", subscribes=["s"], window_seconds=3.0)
    good = _GoodOperator(name="good", subscribes=["s"], window_seconds=3.0)
    actor = ClientTemporalActor(task_id=1, cq=cq, stage="MOCK", operators=[bad, good])
    actor._persist_alarms = lambda alarms: captured.extend(alarms)

    actor._tick()

    # bad 抛异常被隔离，good 仍正常出告警
    assert good._sm.get("ran") is True
    assert len(captured) == 1
    # 别名前烧：告警 .stage 在产出处（_tick）即被烧成 actor 构造期解析的别名，早于 _persist_alarms
    assert captured[0].stage == actor._stage_alias
    cq.set_latest_temporal.assert_called_once()
    assert cq.set_latest_temporal.call_args[0][0] == ["good"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
