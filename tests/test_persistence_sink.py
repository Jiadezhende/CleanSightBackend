"""flush_residual_segments 行为守卫 + persistence 解耦不变式。

覆盖：
1. flush_residual_segments 按 cq.ca_segment_len 切段，块数与旧 _flush_all_remaining_segments 等价；
2. persistence 包源码零 inference import（告警过闸编排移回 alarm_sink 后仍不成反向依赖）。

注：告警过闸+落库编排已移出 PersistenceManager 至 inference/temporal/alarm_sink，
其行为守卫见 test_alarm_sink.py。
"""

import inspect
from unittest.mock import MagicMock

from app.services.persistence.manager import PersistenceManager


def _pm() -> PersistenceManager:
    return PersistenceManager()


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
