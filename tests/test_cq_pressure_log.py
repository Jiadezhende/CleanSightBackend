"""ClientQueues 三条 CA 队列的压力日志测试。

检查点在 append_* 内部（写者线程顺带驱动，不新起线程、不散到 decoder/viz 各生产者），
故这些用例同时也是「日志代码不改变入队/丢帧/返回值行为」的回归。

reporter 的 interval 默认 10s，用例内的连续写落在同一窗口 → 至多一行，正是要锁死的形态。
"""

import logging
import time

import pytest
from factories import make_cq, make_frame

from app.utils.pressure import PRESSURE_LOGGER_NAME as CQ_LOGGER


def _lines(caplog, resource=None):
    out = [r.getMessage() for r in caplog.records if "[PRESSURE]" in r.getMessage()]
    if resource is not None:
        out = [line for line in out if f"resource={resource}" in line]
    return out


def test_processed_line_carries_identity_and_age(caplog):
    """越水位（maxlen 的一半）打一行，带身份 + 深度 + 最老帧年龄。"""
    cq = make_cq(task_id=7, step_id=3, stage="CLEAN", ca_maxlen=10)
    now = time.time()

    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        for i in range(5):
            cq.append_ca_processed(make_frame(ts=now - 3.0 + i * 0.1))

    lines = _lines(caplog, "ca_processed")
    assert len(lines) == 1
    line = lines[0]
    assert "component=client_queues" in line
    assert "task_id=7 step_id=3 stage=CLEAN" in line
    assert "depth=5 capacity=10 utilization=0.500" in line
    assert "oldest_age_ms=3" in line          # ~3000ms，前缀匹配足够
    assert "reason=queue_high_watermark" in line


def test_rate_limited_within_window(caplog):
    """一个 interval 内灌满整条队列也只出一行——过载时不刷屏。"""
    cq = make_cq(ca_maxlen=10)
    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        for _ in range(60):
            cq.append_ca_processed(make_frame(ts=time.time()))

    assert len(_lines(caplog, "ca_processed")) == 1
    assert cq.frames_dropped_processed == 50   # 行为不变：满了照丢照计数


def test_calm_queue_is_silent(caplog):
    """平稳期（水位以下、无丢帧）完全静默。"""
    cq = make_cq(ca_maxlen=100)
    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        for _ in range(30):
            cq.append_ca_processed(make_frame(ts=time.time()))
            cq.append_ca_raw(make_frame(ts=time.time()))

    assert not _lines(caplog)


def test_teardown_is_silent(caplog):
    """run 拆除只静默清计时与基线，不额外打日志。"""
    cq = make_cq(ca_maxlen=10)
    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        for _ in range(6):
            cq.append_ca_processed(make_frame(ts=time.time()))
        before = len(_lines(caplog, "ca_processed"))
        cq.close()

    assert len(_lines(caplog, "ca_processed")) == before
    assert cq._processed_pressure._baselines == {}


def test_raw_and_ready_report_their_own_resource(caplog):
    cq = make_cq(ca_maxlen=10, inference_decimation=1)
    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        for _ in range(6):
            cq.append_ca_raw(make_frame(ts=time.time()))
            cq.append_ca_ready_with_throttle(make_frame(ts=time.time()))

    assert len(_lines(caplog, "ca_raw")) == 1
    assert len(_lines(caplog, "ca_ready")) == 1


def test_drop_growth_reported_even_when_queue_shallow(caplog):
    """还在丢就是还有压力：队列被下游排空、水位一直很浅，只有 drop_delta 能报出来。"""
    cq = make_cq(ca_maxlen=100)   # 水位线 50，下面全程够不着
    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        cq.append_ca_raw(make_frame(ts=time.time()))   # 播种 drop 基线，静默
        assert not _lines(caplog, "ca_raw")

        cq.frames_dropped_raw += 7                     # 排空得快，但仍在丢
        cq.append_ca_raw(make_frame(ts=time.time()))

    lines = _lines(caplog, "ca_raw")
    assert len(lines) == 1
    assert "depth=2 capacity=100" in lines[0]          # 水位极浅，纯靠 delta 触发
    assert "drop_total=7 drop_delta=7" in lines[0]


@pytest.mark.parametrize("maxlen", [3, 10])
def test_behaviour_unchanged_by_logging(caplog, maxlen):
    """日志不改行为：返回值、丢帧计数、队列长度与加日志前一致。"""
    cq = make_cq(ca_maxlen=maxlen, inference_decimation=1)
    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        results = [cq.append_ca_ready_with_throttle(make_frame(ts=time.time()))
                   for _ in range(maxlen + 2)]
        for _ in range(maxlen + 2):
            cq.append_ca_processed(make_frame(ts=time.time()))

    assert results == [True] * maxlen + [False, False]
    assert cq.frames_dropped_ready == 2
    assert len(cq.ca_ready) == maxlen
    assert cq.frames_dropped_processed == 2
    assert cq.get_ca_processed_length() == maxlen


def test_decimated_frames_do_not_observe(caplog):
    """被抽帧丢掉的帧不参与观测（省掉 (N-1)/N 的调用），且不计入 ready 丢帧。"""
    cq = make_cq(ca_maxlen=10, inference_decimation=3)
    with caplog.at_level(logging.INFO, logger=CQ_LOGGER):
        accepted = sum(cq.append_ca_ready_with_throttle(make_frame(ts=time.time()))
                       for _ in range(9))

    assert accepted == 3
    assert cq.frames_dropped_ready == 0
    assert not _lines(caplog, "ca_ready")
