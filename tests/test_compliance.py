"""规则判定 `app.services.inference.offline.compliance`：证据层、五种规则、核对编排、`cli check`。

全部用合成的 `LabelProbs` + `FrameDetection`（15 fps 时间轴，动作区间内该类概率 0.85），不跑模型。
"""

import json

import numpy as np
import pytest

from app.services.inference.offline.compliance import checker
from app.services.inference.offline.compliance.config import parse_config
from app.services.inference.offline.compliance.evidence import LENIENT, STRICT, build_evidence
from app.services.inference.offline.compliance.rules import (
    COMPLIANT,
    SUSPECTED,
    UNDETERMINED,
    parse_rule,
)
from app.storage import inference
from app.types.alarm import ALARM_MODE_SETTLEMENT, AlarmMetric, AlarmType
from app.types.detection import DetBox, DetectorOutput, FrameDetection
from app.types.run import RunIdentity
from app.types.temporal import LabelProbs, TemporalSegment

FPS = 15.0
T0 = 1727000000.0
LABELS = ("idle", "water_injection", "flush", "long_brush_insert", "long_brush_withdraw", "short_brush_cleaning")


def _probs(intervals, duration_s, p=0.85):
    """intervals: [(label, start_s, end_s[, p])]，相对 T0；区间外为 idle。"""
    ts = T0 + np.arange(int(duration_s * FPS)) / FPS
    probs = np.zeros((ts.size, len(LABELS)))
    probs[:, 0] = 1.0
    for item in intervals:
        label, a, b = item[:3]
        pv = item[3] if len(item) > 3 else p
        m = (ts >= T0 + a) & (ts < T0 + b)
        probs[m] = 0.0
        probs[m, LABELS.index(label)] = pv
        probs[m, 0] = 1.0 - pv
    return LabelProbs(ts=ts, probs=probs, labels=LABELS)


def _frames(duration_s, hidden=(), extra_s=0.0):
    """每帧一个 scope_control_body 框；hidden 区间内无框（不可见）；extra_s：检测比概率多出的尾巴（截断）。"""
    out = []
    for t in T0 + np.arange(int((duration_s + extra_s) * FPS)) / FPS:
        rel = t - T0
        boxes = [] if any(a <= rel < b for a, b in hidden) else [DetBox([0, 0, 10, 10], 0.9, 1, "scope_control_body")]
        out.append(FrameDetection(
            ts=float(t), by_source={"clean_large": DetectorOutput(boxes=boxes, metadata={}, timestamp=float(t))},
            frame_width=640, frame_height=480,
        ))
    return out


def _passes(n, start=60.0, ins=3.0, wd=2.0, rest=1.5):
    """n 次长刷往返：insert ins 秒 → withdraw wd 秒 → idle rest 秒。"""
    out, t = [], start
    for _ in range(n):
        out += [("long_brush_insert", t, t + ins), ("long_brush_withdraw", t + ins, t + ins + wd)]
        t += ins + wd + rest
    return out


SPEC = {
    "evidence": {
        "smooth_s": 1.0, "p_floor": 0.3,
        "visible_classes": ["scope_control_body", "scope_mid_section"],
        "visible_window_s": 1.0, "gap_s": 2.0,
        "episodes": {
            "strict": {"p_on": 0.6, "p_off": 0.4, "merge_gap_s": 1.5, "min_len_s": 1.5},
            "lenient": {"p_on": 0.4, "p_off": 0.25, "merge_gap_s": 0.5, "min_len_s": 0.5},
        },
    },
    "groups": {
        "brushing": ["long_brush_insert", "long_brush_withdraw"],
        "insert": ["long_brush_insert"],
        "withdraw": ["long_brush_withdraw"],
        "short_brush": ["short_brush_cleaning"],
        "flush": ["flush"],
    },
    "rules": [
        {"id": "clean.short_brush.duration", "type": "duration", "group": "short_brush", "min_s": 10.0,
         "min_visible_ratio": 0.6, "message": "短毛刷刷洗约 {duration_s:.0f} s，不足 10 s"},
        {"id": "clean.long_brush.count", "type": "count", "group": "brushing", "unit": "round_trip",
         "forward": "insert", "backward": "withdraw", "min_count": 4, "min_visible_ratio": 0.6,
         "message": "长毛刷刷洗 {count} 次，少于 4 次"},
        {"id": "clean.brush.order", "type": "order", "before": "short_brush", "after": "brushing",
         "before_q": 0.9, "after_q": 0.1, "present_s": 2.0, "margin_s": 3.0,
         "message": "长毛刷刷洗（{at} 起）早于短毛刷刷洗完成"},
        {"id": "clean.flush.missing", "type": "presence", "group": "flush", "present_s": 5.0, "absent_s": 1.0,
         "min_visible_ratio": 0.6, "message": "未检测到灌注清洗液（可见 {visible_ratio:.0%}）"},
        {"id": "clean.flush.wait", "type": "wait", "after": "flush", "until": ["short_brush", "brushing"],
         "min_s": 15.0, "min_visible_ratio": 0.8, "message": "灌注后 {wait_s:.0f} s 即开始刷洗（{at}）"},
    ],
}

COMPLIANT_RUN = [
    ("short_brush_cleaning", 5.0, 17.0),
    ("flush", 20.0, 30.0),
    *_passes(4, start=50.0),
]


def _verdicts(intervals, duration_s=120.0, frames=None, spec=SPEC):
    config = parse_config(spec)
    _, verdicts = checker.evaluate(config, _probs(intervals, duration_s), frames or _frames(duration_s))
    return {v.rule_id: v for v in verdicts}


def _ev(intervals, duration_s=120.0, frames=None):
    config = parse_config(SPEC)
    return build_evidence(_probs(intervals, duration_s), frames or _frames(duration_s), config.groups,
                                  config.evidence)


# ---------------------------------------------------------------------------
# 证据层
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_label_not_in_model_raises(self):
        spec = {**SPEC, "groups": {**SPEC["groups"], "flush": ["no_such_label"]}}
        with pytest.raises(ValueError, match="no_such_label"):
            _verdicts(COMPLIANT_RUN, spec=spec)

    def test_fragmented_and_continuous_flush_have_similar_evidence(self):
        whole = _ev([("flush", 20.0, 30.0)]).evidence_s("flush")
        pieces = [("flush", 20.0 + i, 20.0 + i + 0.8) for i in range(10)]   # 10 段 0.8 s，中间 0.2 s 空
        frag = _ev(pieces).evidence_s("flush")
        assert whole == pytest.approx(10 * 0.85, rel=0.1)
        assert frag == pytest.approx(whole * 0.8, rel=0.15)

    def test_background_noise_does_not_accumulate(self):
        """600 s 上 0.05 的底噪不计证据（p_floor）。"""
        assert _ev([("flush", 0.0, 600.0, 0.05)], duration_s=600.0).evidence_s("flush") == 0.0

    def test_hidden_span_and_truncated_tail_lower_visible_ratio(self):
        assert _ev([], frames=_frames(120.0)).visible_ratio() == pytest.approx(1.0, abs=0.02)
        assert _ev([], frames=_frames(120.0, hidden=[(0.0, 60.0)])).visible_ratio() == pytest.approx(0.5, abs=0.03)
        assert _ev([], frames=_frames(120.0, extra_s=120.0)).visible_ratio() == pytest.approx(0.5, abs=0.03)

    def test_round_trips_counted(self):
        ev = _ev(_passes(4))
        assert len(ev.round_trips("insert", "withdraw", STRICT)) == 4
        assert len(ev.round_trips("insert", "withdraw", LENIENT)) == 4

    def test_round_trips_without_rest_between_passes(self):
        assert len(_ev(_passes(4, rest=0.0)).round_trips("insert", "withdraw", STRICT)) == 4

    def test_direction_blips_inside_insert_do_not_add_trips(self):
        """插入段里夹 0.2 s 的 withdraw 误判，不多计往返。"""
        blips = []
        for a, b in [(p[1], p[2]) for p in _passes(4) if p[0] == "long_brush_insert"]:
            blips.append(("long_brush_withdraw", a + 1.0, a + 1.2))
        assert len(_ev(_passes(4) + blips).round_trips("insert", "withdraw", STRICT)) == 4


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


class TestRules:
    def test_compliant_run_is_all_compliant(self):
        assert {k: v.state for k, v in _verdicts(COMPLIANT_RUN).items()} == {
            "clean.short_brush.duration": COMPLIANT,
            "clean.long_brush.count": COMPLIANT,
            "clean.brush.order": COMPLIANT,
            "clean.flush.missing": COMPLIANT,
            "clean.flush.wait": COMPLIANT,
        }

    def test_short_brush_too_short(self):
        run = [("short_brush_cleaning", 5.0, 13.0)] + COMPLIANT_RUN[1:]
        v = _verdicts(run)["clean.short_brush.duration"]
        assert v.state == SUSPECTED and v.values["duration_s"] < 10.0

    def test_short_brush_near_threshold_is_undetermined(self):
        """strict 只认 0.85 的核心 9.4 s，lenient 连 0.5 的前缀共约 10.9 s：两套跨过阈值。"""
        run = [("short_brush_cleaning", 3.5, 5.0, 0.5), ("short_brush_cleaning", 5.0, 14.4)] + COMPLIANT_RUN[1:]
        v = _verdicts(run)["clean.short_brush.duration"]
        assert v.state == UNDETERMINED
        assert v.values["duration_strict_s"] < 10.0 <= v.values["duration_lenient_s"]

    def test_too_few_long_brush_trips(self):
        run = COMPLIANT_RUN[:2] + _passes(2, start=50.0)
        v = _verdicts(run)["clean.long_brush.count"]
        assert v.state == SUSPECTED and v.values["count"] == 2

    def test_no_long_brush_at_all_is_count_zero(self):
        v = _verdicts(COMPLIANT_RUN[:2])["clean.long_brush.count"]
        assert v.state == SUSPECTED and v.values["count"] == 0

    def test_long_brush_before_short_brush(self):
        run = [("flush", 20.0, 30.0), *_passes(4, start=50.0), ("short_brush_cleaning", 90.0, 102.0)]
        v = _verdicts(run)["clean.brush.order"]
        assert v.state == SUSPECTED and v.at == pytest.approx(T0 + 50.0, abs=3.0)

    def test_stray_short_brush_fragment_after_long_does_not_flip_order(self):
        v = _verdicts(COMPLIANT_RUN + [("short_brush_cleaning", 100.0, 100.6)])["clean.brush.order"]
        assert v.state == COMPLIANT

    def test_flush_missing(self):
        assert _verdicts([COMPLIANT_RUN[0], *COMPLIANT_RUN[2:]])["clean.flush.missing"].state == SUSPECTED

    def test_flush_missing_but_mostly_hidden_is_undetermined(self):
        frames = _frames(120.0, hidden=[(0.0, 80.0)])
        v = _verdicts([COMPLIANT_RUN[0], *COMPLIANT_RUN[2:]], frames=frames)["clean.flush.missing"]
        assert v.state == UNDETERMINED

    def test_wait_too_short(self):
        run = [("short_brush_cleaning", 5.0, 17.0), ("flush", 20.0, 30.0), *_passes(4, start=38.0)]
        v = _verdicts(run)["clean.flush.wait"]
        assert v.state == SUSPECTED and v.values["wait_s"] < 15.0
        assert v.at == pytest.approx(T0 + 38.0, abs=1.0)

    def test_wait_without_following_brush_is_undetermined(self):
        run = [("short_brush_cleaning", 5.0, 17.0), *_passes(4, start=30.0), ("flush", 100.0, 110.0)]
        assert _verdicts(run)["clean.flush.wait"].state == UNDETERMINED


class TestParse:
    def test_valid_spec(self):
        config = parse_config(SPEC)
        assert [r.id for r in config.rules] == [r["id"] for r in SPEC["rules"]]

    def test_missing_or_empty_rules_means_no_check(self):
        assert parse_config({}) is None
        assert parse_config({**SPEC, "rules": []}) is None

    @pytest.mark.parametrize("bad, match", [
        ({"type": "nope"}, "未知 type"),
        ({"bogus": 1}, "未知字段"),
        ({"message": "{nope}"}, "占位"),
        ({"group": "undefined_group"}, "未定义的组"),
    ])
    def test_bad_rule_raises(self, bad, match):
        rule = {**SPEC["rules"][3], **bad}
        with pytest.raises(ValueError, match=match):
            parse_config({**SPEC, "rules": [rule]})

    def test_duplicate_ids_raise(self):
        with pytest.raises(ValueError, match="重复"):
            parse_config({**SPEC, "rules": [SPEC["rules"][3], SPEC["rules"][3]]})

    def test_round_trip_needs_forward_backward(self):
        with pytest.raises(ValueError, match="forward"):
            parse_rule({"id": "c", "type": "count", "group": "brushing", "unit": "round_trip", "min_count": 4,
                        "message": "x"})


# ---------------------------------------------------------------------------
# 核对编排（落盘）
# ---------------------------------------------------------------------------


RUN_ID = 7


def _run():
    from app.storage.utils import root as _root

    run = RunIdentity(1, 2, RUN_ID)
    _root.run_path(run).mkdir(parents=True, exist_ok=True)
    return run


def _check(run, spec=SPEC, **kw):
    return checker.check_run(run, spec, stage="CLEAN", **kw)


def _seed(run, intervals, duration_s=120.0, probs=True):
    inference.append_detections(run, _frames(duration_s))
    inference.write_temporal(run, [TemporalSegment(producer="Seg", label="flush", start=T0, end=T0 + 1)])
    if probs:
        inference.write_label_probs(run, _probs(intervals, duration_s))


class TestCheckRun:
    def test_writes_suspected_as_alarms(self, tmp_storage):
        run = _run()
        _seed(run, [COMPLIANT_RUN[0], *COMPLIANT_RUN[2:]])   # 去掉灌注
        result = _check(run)
        alarms = inference.read_alarms(run)
        assert result.status == "completed" and result.alarm_count == len(alarms) >= 1
        missing = [a for a in alarms if a.metadata["rule_id"] == "clean.flush.missing"]
        assert len(missing) == 1
        a = missing[0]
        assert a.metric is AlarmMetric.STEP_MISSING and a.alarm_type is AlarmType.PROCESS_VIOLATION
        assert (a.mode, a.stage, a.alarm_level) == (ALARM_MODE_SETTLEMENT, "CLEAN", "warning")
        assert a.metadata["producer"] == "Seg" and isinstance(a.metadata["coverage_end_ms"], int)
        assert a.alarm_message.startswith("未检测到灌注清洗液")

    def test_compliant_run_writes_empty_file(self, tmp_storage):
        run = _run()
        _seed(run, COMPLIANT_RUN)
        assert _check(run).alarm_count == 0
        assert inference.read_alarms(run) == []

    def test_dry_run_writes_nothing(self, tmp_storage):
        run = _run()
        _seed(run, COMPLIANT_RUN[:1])
        result = _check(run, dry_run=True)
        assert len(result.verdicts) == len(SPEC["rules"]) and result.alarm_count >= 1
        assert not _alarms_path(tmp_storage).exists()

    def test_no_rules_skips_and_keeps_old_alarms(self, tmp_storage):
        run = _run()
        _seed(run, COMPLIANT_RUN[:1])
        _check(run)
        before = inference.read_alarms(run)
        result = _check(run, spec={})
        assert (result.status, result.alarm_count) == ("skipped", None)
        assert len(inference.read_alarms(run)) == len(before) >= 1

    def test_no_label_probs_skips(self, tmp_storage):
        run = _run()
        _seed(run, COMPLIANT_RUN, probs=False)
        assert _check(run).status == "skipped"
        assert not _alarms_path(tmp_storage).exists()


def _alarms_path(root):
    return root / "1" / "2" / str(RUN_ID) / "inference" / "alarms.jsonl"


class TestCheckCli:
    def _patch_config(self, monkeypatch, spec=SPEC):
        from app.services.inference import config as config_mod
        from app.services.inference.config import InferenceConfig

        cfg = InferenceConfig({"stages": {"2": {"alias": "CLEAN", "compliance": spec}}})
        monkeypatch.setattr(config_mod, "load_stage_config", lambda *a, **k: cfg)

    def test_check_writes_alarms(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import cli

        run = _run()
        _seed(run, COMPLIANT_RUN[:1])
        self._patch_config(monkeypatch)
        rc = cli.main(["check", "--task-id", "1", "--step-id", "2"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 0
        assert (payload["status"], payload["run_id"]) == ("completed", RUN_ID)
        assert payload["alarm_count"] == len(inference.read_alarms(run)) >= 1

    def test_check_dry_run_prints_every_verdict(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import cli

        _seed(_run(), COMPLIANT_RUN)
        self._patch_config(monkeypatch)
        assert cli.main(["check", "--task-id", "1", "--step-id", "2", "--dry-run"]) == 0
        lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
        assert [x["rule_id"] for x in lines[:-1]] == [r["id"] for r in SPEC["rules"]]
        assert lines[-1]["alarm_count"] == 0 and not _alarms_path(tmp_storage).exists()

    def test_check_bad_config_exits_nonzero(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import cli

        _seed(_run(), COMPLIANT_RUN)
        self._patch_config(monkeypatch, spec={**SPEC, "rules": [{**SPEC["rules"][3], "type": "nope"}]})
        assert cli.main(["check", "--task-id", "1", "--step-id", "2"]) == 1
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "error"

    def test_check_without_run_exits_nonzero(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import cli

        self._patch_config(monkeypatch)
        assert cli.main(["check", "--task-id", "9", "--step-id", "2"]) == 1


def test_repo_config_compliance_parses():
    """仓库里 stage "2" 的规则表能解析（YAML 写错要在测试里炸，不等到离线作业）。"""
    from app.services.inference.config import load_stage_config

    config = parse_config(load_stage_config().get_stage_config("2").compliance)
    assert config is not None and len(config.rules) == 5
