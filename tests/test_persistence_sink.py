"""persist_alarms / flush_residual_segments 迁入 PersistenceManager 后的行为守卫。

覆盖三条不变式：
1. persist_alarms 直接读 alarm.stage（别名已由 actor 前烧），不反向解析 alias；
2. flush_residual_segments 按 cq.ca_segment_len 切段，块数与旧 _flush_all_remaining_segments 等价；
3. persistence 包源码零 inference import（sink 下沉后不成反向依赖）。
"""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.domain.alarm import Alarm, AlarmMetric, AlarmType
from app.services.persistence.manager import PersistenceManager


def _pm() -> PersistenceManager:
    return PersistenceManager()


def test_persist_alarms_reads_baked_stage():
    pm = _pm()
    captured = []
    pm.persist_alarm = lambda d: captured.append(d) or True

    cq = MagicMock()
    cq.task_id = 7
    cq.get_task.return_value = SimpleNamespace(current_step="1")
    cq.append_alarm_record_with_gate.return_value = True  # 过闸

    alarm = Alarm(
        alarm_type=AlarmType.MOCK, alarm_level="low", alarm_message="m",
        metric=AlarmMetric.BUBBLE,
    )
    alarm.stage = "长刷"  # 产出方已烧的可读别名

    pm.persist_alarms([alarm], cq=cq, client_id="c", mode="SETTLEMENT")

    assert len(captured) == 1
    assert captured[0]["stage"] == "长刷"          # 直接读 alarm.stage，未做 alias 解析
    assert captured[0]["alarm_metric"] == AlarmMetric.BUBBLE
    assert alarm.mode == "SETTLEMENT"              # mode 由 sink 补


def test_persist_alarms_gate_reject_skips_persist():
    pm = _pm()
    captured = []
    pm.persist_alarm = lambda d: captured.append(d) or True

    cq = MagicMock()
    cq.task_id = 7
    cq.get_task.return_value = SimpleNamespace(current_step="1")
    cq.append_alarm_record_with_gate.return_value = False  # 冷却窗口拦截

    alarm = Alarm(alarm_type=AlarmType.MOCK, alarm_level="low", alarm_message="m")
    pm.persist_alarms([alarm], cq=cq, client_id="c", mode="REALTIME")

    assert captured == []  # 被闸门挡下，不落库


def test_flush_residual_segments_chunks_by_seg_len():
    pm = _pm()
    calls = []
    pm.persist_hls_segment = lambda **kw: calls.append(kw) or True

    cq = MagicMock()
    cq.task_id = 7
    cq.step_id = 1
    cq.ca_segment_len = 10
    cq.drain_ca_raw.return_value = list(range(25))       # 25 → 3 段 (10,10,5)
    cq.drain_ca_processed.return_value = list(range(10))  # 10 → 1 段

    pm.flush_residual_segments(cq)

    raw = [c for c in calls if c["segment_type"] == "raw"]
    proc = [c for c in calls if c["segment_type"] == "processed"]
    assert len(raw) == 3
    assert [len(c["frames"]) for c in raw] == [10, 10, 5]
    assert len(proc) == 1
    assert all(c["task_id"] == 7 and c["step_id"] == 1 for c in calls)


def test_flush_residual_segments_missing_keys_early_return():
    pm = _pm()
    pm.persist_hls_segment = MagicMock()

    cq = MagicMock()
    cq.task_id = None  # 无 task_id → 早退
    pm.flush_residual_segments(cq)

    pm.persist_hls_segment.assert_not_called()


def test_persistence_manager_source_has_no_inference_import():
    # sink 下沉后，persistence 不得反向依赖 inference（别名前烧的目的）。
    src = inspect.getsource(inspect.getmodule(PersistenceManager))
    assert "app.services.inference" not in src
