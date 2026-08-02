"""PressureReporter 测试（压力日志的单一真源）。

契约很小：**到点（interval）且有压力才打一行**，平稳时静默，delta 相对上次打印。
时钟注入假钟，避免 sleep 与时序抖动。
"""

import logging

import pytest

from app.utils.pressure import PRESSURE_LOGGER_NAME, PressureReporter

LOGGER_NAME = PRESSURE_LOGGER_NAME


class _FakeClock:
    """可手推的单调钟。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return _FakeClock()


@pytest.fixture
def reporter(clock):
    return PressureReporter(
        "unit", "queue",
        interval=10.0,
        identity={"task_id": 7, "step_id": None},   # None 身份字段不应出现在日志里
        clock=clock,
    )


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "[PRESSURE]" in r.getMessage()]


def test_calm_is_silent(reporter, clock, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        for _ in range(100):
            reporter.observe(False, depth=1, capacity=256)
            clock.advance(1.0)

    assert not _lines(caplog)


def test_rate_limited_to_one_line_per_interval(reporter, clock, caplog):
    """压力持续 100 次 observe / 5 秒内只出一条——过载时日志系统不会先被打满。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        for _ in range(100):
            reporter.observe(True, depth=180, capacity=256)
            clock.advance(0.05)   # 共 5s，< interval

    lines = _lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("[PRESSURE] component=unit resource=queue task_id=7")
    assert "step_id" not in lines[0]      # 不适用字段直接不打，不用魔法值
    assert "depth=180 capacity=256" in lines[0]


def test_periodic_while_pressured(reporter, clock, caplog):
    """压力持续 35 秒（interval=10s）→ 4 条周期快照。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        for _ in range(35):
            reporter.observe(True, depth=200, capacity=256)
            clock.advance(1.0)

    assert len(_lines(caplog)) == 4


def test_level_is_warning(reporter, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(True, depth=200, capacity=256)

    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_delta_is_since_last_print_not_since_last_observe(reporter, clock, caplog):
    """delta 的基线只在**实际打印**后推进——否则限频窗内的 observe 会把增量吃掉。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(True, depth=200, capacity=256, drop_total=100)   # 打印①
        clock.advance(4.0)
        reporter.observe(True, depth=200, capacity=256, drop_total=140)   # 限频内，静默
        clock.advance(7.0)
        reporter.observe(True, depth=200, capacity=256, drop_total=180)   # 打印②

    lines = _lines(caplog)
    assert "drop_total=100 drop_delta=0" in lines[0]     # 首见：只播种基线
    assert "drop_total=180 drop_delta=80" in lines[1]    # 100→180，跨越中间那次静默 observe


def test_growing_counter_alone_is_pressure(reporter, clock, caplog):
    """谓词为假但累计计数仍在涨 → 仍算有压力（丢完就空，水位测不到）。

    此时 reason 必须如实报 counter_growth：谓词没响却挂调用方的水位 reason，会打出
    `utilization=0.000 ... reason=queue_high_watermark` 这种自相矛盾的行。
    """
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(
            False, reason="queue_high_watermark", depth=0, capacity=256, drop_total=10,
        )  # 首次：播种基线，不报
        assert not _lines(caplog)
        clock.advance(11.0)
        reporter.observe(
            False, reason="queue_high_watermark", depth=0, capacity=256, drop_total=25,
        )  # 还在丢 → 报

    lines = _lines(caplog)
    assert len(lines) == 1
    assert "drop_delta=15" in lines[0]
    assert "reason=counter_growth" in lines[0]
    assert "queue_high_watermark" not in lines[0]


def test_predicate_wins_reason_when_both_fire(reporter, clock, caplog):
    """谓词与计数增长同时成立 → reason 取调用方的（水位是更强信号，增量由 *_delta 自明）。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(
            True, reason="queue_high_watermark", depth=200, capacity=256, drop_total=10,
        )
        clock.advance(11.0)
        reporter.observe(
            True, reason="queue_high_watermark", depth=240, capacity=256, drop_total=25,
        )

    lines = _lines(caplog)
    assert "reason=queue_high_watermark" in lines[-1]
    assert "drop_delta=15" in lines[-1]


def test_first_sight_of_counter_does_not_replay_history(reporter, caplog):
    """首次见到某累计计数只播种基线，不把历史累计当成"刚刚丢的"。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(False, depth=0, capacity=256, drop_total=5000)

    assert not _lines(caplog)


def test_pressure_ends_by_going_silent(reporter, clock, caplog):
    """压力解除就是"不再出现新行"——不打恢复行。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(True, depth=200, capacity=256)
        clock.advance(30.0)
        for _ in range(30):
            reporter.observe(False, depth=0, capacity=256)
            clock.advance(1.0)

    assert len(_lines(caplog)) == 1


def test_reset_clears_timer_and_baseline(reporter, clock, caplog):
    """reset（run 拆除）后重新计时：不用等满一个 interval 才肯再报。"""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(True, depth=200, capacity=256, drop_total=10)
        reporter.reset()
        reporter.observe(True, depth=200, capacity=256, drop_total=10)

    lines = _lines(caplog)
    assert len(lines) == 2
    assert "drop_delta=0" in lines[1]     # 基线重新播种，不把旧累计当增量


def test_observe_never_raises(reporter, caplog):
    """日志异常绝不外泄——热路径（入队/提交/写回）不能被日志代码打断。"""

    class Exploding:
        def __format__(self, spec):
            raise RuntimeError("boom")

        def __str__(self):
            raise RuntimeError("boom")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(True, depth=Exploding(), capacity=256)   # 不抛


def test_none_fields_omitted(reporter, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporter.observe(True, depth=5, capacity=10, oldest_age_ms=None, utilization=0.5)

    line = _lines(caplog)[0]
    assert "oldest_age_ms" not in line
    assert "utilization=0.500" in line
