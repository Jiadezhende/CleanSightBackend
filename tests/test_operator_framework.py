"""流处理框架能力测试：subscribes 注入 / 感受野 _clip / 帧窗投影 / 缓冲按感受野 / per-operator 隔离。"""

from typing import List
from unittest.mock import MagicMock

import pytest

from factories import make_bare_cq, make_frame_detections, make_frame_feature
from app.services.inference.config import load_stage_config
from app.domain.alarm import Alarm, AlarmType
from app.services.inference.stage_factory import StageFactory
from app.services.inference.temporal.operator import Operator


def _out(ts: float, n: int = 1):
    return make_frame_detections(n=n, class_name="x", ts=ts)


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
    win = [make_frame_feature(source="s", ts=t) for t in [0.0, 1.0, 2.0, 3.0, 4.0]]  # latest=4.0
    op_small = _NoopOperator(name="a", subscribes=["s"], window_seconds=2.0)
    op_big = _NoopOperator(name="b", subscribes=["s"], window_seconds=5.0)
    # 感受野 2s → 保留 ts>=2.0：2,3,4
    assert [f.ts for f in op_small._clip(win)] == [2.0, 3.0, 4.0]
    # 感受野 5s → 全保留
    assert [f.ts for f in op_big._clip(win)] == [0.0, 1.0, 2.0, 3.0, 4.0]


# ========== 帧窗投影（单订阅取自身流，多流已在写回口对齐）==========


def test_primary_window_projects_subscribed_source():
    op = _NoopOperator(name="p", subscribes=["a"], window_seconds=10.0)
    # 每帧 by_source 同时含 a/b（写回口已对齐）；primary_window 只投影订阅的 a
    win = [
        make_frame_feature(ts=t, by_source={"a": _out(t), "b": _out(t, n=2)})
        for t in [1.0, 2.0, 3.0]
    ]
    projected = op.primary_window(win)
    assert [fd.timestamp for fd in projected] == [1.0, 2.0, 3.0]
    assert all(fd.detections[0].class_name == "x" for fd in projected)


def test_primary_window_skips_frames_missing_source():
    op = _NoopOperator(name="p", subscribes=["a"], window_seconds=10.0)
    win = [
        make_frame_feature(ts=1.0, by_source={"a": _out(1.0)}),
        make_frame_feature(ts=2.0, by_source={"b": _out(2.0)}),  # 无 a → 跳过
    ]
    assert [fd.timestamp for fd in op.primary_window(win)] == [1.0]


# ========== subscribes 必填 ==========


def test_subscribes_required():
    with pytest.raises(ValueError):
        _NoopOperator(name="x", subscribes=[], window_seconds=3.0)


# ========== 缓冲按感受野（max(底线, 感受野)）==========


def test_stream_buffer_floor_10s():
    cq = make_bare_cq()
    for t in [0.0, 5.0, 10.0, 15.0, 20.0]:
        cq.push_detection(make_frame_feature(source="x", ts=t))
    # 未配感受野 → 底线 10s：cutoff=20-10=10 → 保留 10,15,20
    win = cq.get_slide_window()
    assert [f.ts for f in win] == [10.0, 15.0, 20.0]


def test_stream_buffer_extends_with_receptive_field():
    cq = make_bare_cq()
    cq.set_stream_windows({"x": 30.0})  # 感受野 30s > 底线
    for t in [0.0, 5.0, 10.0, 15.0, 20.0]:
        cq.push_detection(make_frame_feature(source="x", ts=t))
    # retain=max(10,30)=30：cutoff=20-30=-10 → 全保留
    win = cq.get_slide_window()
    assert [f.ts for f in win] == [0.0, 5.0, 10.0, 15.0, 20.0]


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
    cq.get_slide_window.return_value = [make_frame_feature(source="s", ts=1.0)]
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
