"""可视化吞吐量观测（[VIZ_THROUGHPUT]）测试。

覆盖 VisualizationWorker._log_throughput_snapshot 的"仅压力时打印"逻辑与三侧归因：
- 产出接近目标 fps → 静默（不刷屏）
- 产出明显低于目标 + 空转占比高 → 打印，标 supply-bound
- 单帧渲染耗时逼近出帧间隔 → 打印，标 render-bound
- 本线程 tick 率不足标称轮询率 → 打印，标 viz-starved（worker 级信号）
- run 在窗中途才起来 → 按其存活跨度算 out_fps，不误报
"""

import threading
from unittest.mock import patch

from app.services.inference.visualization.worker import VisualizationWorker

# 统计窗起点：测试里给个固定墙钟，令 first_seen 可按"距窗末多少秒"反推
_WIN_START = 1000.0


def _worker(target_fps: float = 20.0, output_fps: float = None) -> VisualizationWorker:
    w = VisualizationWorker(
        stop_event=threading.Event(),
        tick_interval=1.0 / target_fps,
        output_fps=output_fps,
    )
    w._win_start = _WIN_START
    return w


def test_snapshot_silent_when_healthy():
    """产出≈目标 fps、tick 转满、渲染快 → 不应打印 [VIZ_THROUGHPUT]。"""
    w = _worker(target_fps=20.0)
    window = 10.0
    w._tick_count = 200               # 20Hz×10s，转满
    # 10s 内渲染 200 帧 = 20fps，几乎不空转
    w._stat_rendered["c1"] = 200
    w._stat_stale["c1"] = 0
    w._render_calls = 200
    w._render_time_sum = 200 * 0.002  # 平均 2ms
    w._render_time_max = 0.004        # 峰值 4ms ≪ 50ms 预算

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_not_called()


def test_snapshot_logs_supply_bound():
    """tick 转满、渲染很快，但产出仅 ~10fps + 大量空转 → 打印并标 supply-bound。"""
    w = _worker(target_fps=20.0)
    window = 10.0
    w._tick_count = 200               # tick 清白
    # 渲染 100 帧 = 10fps；另有 100 次空转（有推理但无新结果）
    w._stat_rendered["c1"] = 100
    w._stat_stale["c1"] = 100
    w._render_calls = 100
    w._render_time_sum = 100 * 0.002  # 平均 2ms，渲染清白
    w._render_time_max = 0.005        # 峰值 5ms ≪ 50ms 预算

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_called_once()
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "[VIZ_THROUGHPUT]" in rendered
    assert "out=10.0fps" in rendered
    assert "supply-bound" in rendered
    assert "viz-starved" not in rendered


def test_snapshot_logs_render_bound():
    """单帧渲染峰值 ≥ 出帧间隔 → 打印并标 render-bound。"""
    w = _worker(target_fps=20.0)
    window = 10.0
    w._tick_count = 200               # tick 清白，亏空不该记到线程头上
    # 产出低（渲染慢导致），且渲染峰值 60ms ≥ 50ms 预算
    w._stat_rendered["c1"] = 90
    w._stat_stale["c1"] = 110
    w._render_calls = 90
    w._render_time_sum = 90 * 0.045   # 平均 45ms
    w._render_time_max = 0.060        # 峰值 60ms ≥ 50ms 预算

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_called_once()
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "[VIZ_THROUGHPUT]" in rendered
    assert "render-bound" in rendered


def test_snapshot_silent_when_oversampled_healthy():
    """过采样（轮询 30Hz、出帧率 15fps）且出帧健康 → 不应误报 supply-bound。

    回归：修复前基准取轮询率，out_fps(15) < 30*0.8=24 恒真 → 告警常亮。
    修复后基准取 output_fps(15)，15 < 15*0.8=12 为假 → 静默。
    """
    w = _worker(target_fps=30.0, output_fps=15.0)
    window = 10.0
    w._tick_count = 300
    # 300 ticks：150 渲染（=15fps，出帧满额）+ 150 空转（过采样必然的空转 tick）
    w._stat_rendered["c1"] = 150
    w._stat_stale["c1"] = 150
    w._render_calls = 150
    w._render_time_sum = 150 * 0.002  # 平均 2ms
    w._render_time_max = 0.005        # 峰值 5ms ≪ 33ms 预算

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_not_called()


def test_snapshot_oversampled_still_flags_real_shortfall():
    """过采样下真出现出帧亏空（8fps ≪ 15fps 期望）→ 仍应报 supply-bound。"""
    w = _worker(target_fps=30.0, output_fps=15.0)
    window = 10.0
    w._tick_count = 300
    # 300 ticks：仅 80 渲染（=8fps < 15*0.8=12）+ 220 空转
    w._stat_rendered["c1"] = 80
    w._stat_stale["c1"] = 220
    w._render_calls = 80
    w._render_time_sum = 80 * 0.002
    w._render_time_max = 0.005        # 渲染清白 → 归因 supply

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_called_once()
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "out=8.0fps" in rendered
    assert "supply-bound" in rendered


def test_snapshot_oversampled_render_spike_over_tick_is_still_supply_bound():
    """过采样下渲染峰值超「轮询间隔」但仍在「出帧间隔」内 + 供给亏空 → 应报 supply-bound 而非 render-bound。

    复现真实日志：poll=30Hz(tick 33ms)、target=15fps(出帧间隔 66ms)，render max 51.7ms
    （>33ms tick 但 <66ms 出帧间隔），out=10.6fps、stale=64%。渲染够快支撑 15fps，亏空来自
    上游供帧——render_bound 的预算须用出帧间隔，否则单帧越过 tick 就被误标 render-bound。
    """
    w = _worker(target_fps=30.0, output_fps=15.0)
    window = 10.0
    w._tick_count = 300
    # 300 ticks：106 渲染(=10.6fps) + 194 空转(=64.7%)
    w._stat_rendered["119"] = 106
    w._stat_stale["119"] = 194
    w._render_calls = 106
    w._render_time_sum = 106 * 0.0185   # 平均 18.5ms « 66ms 出帧间隔
    w._render_time_max = 0.0517         # 峰值 51.7ms：> 33ms tick 但 < 66ms 出帧间隔

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_called_once()
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "out=10.6fps" in rendered
    assert "supply-bound" in rendered
    assert "render-bound" not in rendered


def test_snapshot_flags_viz_starved():
    """tick 率塌到标称轮询率的 1/10 → 打印并标 viz-starved，不得错记成 supply-bound。

    回归 issue #82：这正是旧 `total >= expected_ticks*0.3` 门槛会咽掉的场景——
    viz 单线程被 GIL 争用饿着，tick 数与产出一起塌，旧逻辑当成"流刚起来"静默。
    """
    w = _worker(target_fps=30.0, output_fps=15.0)
    window = 10.0
    w._tick_count = 30                # 只转了 3Hz « 30Hz 标称
    # 转一次渲一次：产出 3fps，几乎不空转（新推理早就在槽里等着了）
    w._stat_rendered["c1"] = 30
    w._stat_stale["c1"] = 0
    w._first_seen["c1"] = _WIN_START           # 整窗存活
    w._last_seen["c1"] = _WIN_START + window
    w._render_calls = 30
    w._render_time_sum = 30 * 0.002   # 渲染本身很快，不是 render-bound
    w._render_time_max = 0.005

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_called_once()
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "ticks=30/300" in rendered
    assert "viz-starved" in rendered
    assert "supply-bound" not in rendered   # 归因不得落到上游


def test_snapshot_silent_for_just_started_run():
    """run 在窗末尾才起来 → 按其观测跨度算 out_fps，不误报 supply-bound。

    旧实现 out_fps = rendered/window 拿整窗当分母，2s 内跑满 15fps 会被算成 3fps；
    历史上那道 0.3 门槛就是为挡这个而设。分母改对后不需要门槛也不误报。
    """
    w = _worker(target_fps=30.0, output_fps=15.0)
    window = 10.0
    w._tick_count = 300                          # viz 线程健康
    w._first_seen["c1"] = _WIN_START + 8.0       # 窗末 2s 才首见
    w._last_seen["c1"] = _WIN_START + window     # 一直活到窗末
    # 2s × 15fps = 30 帧，出帧其实是满的
    w._stat_rendered["c1"] = 30
    w._stat_stale["c1"] = 30
    w._render_calls = 30
    w._render_time_sum = 30 * 0.002
    w._render_time_max = 0.005

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_not_called()


def test_snapshot_silent_for_run_terminated_mid_window():
    """run 在窗中途被 terminate → 分母取到末见为止，不误报 supply-bound。

    回归真实日志（2026-07-28 16:01:49）：task 119 在窗内第 3s 被停，整窗内跑满 15fps，
    但分母若取「首见→窗末」(10s) 会被算成 5.0fps 并误标 supply-bound。
    """
    w = _worker(target_fps=30.0, output_fps=15.0)
    window = 10.0
    w._tick_count = 300
    w._first_seen["c1"] = _WIN_START             # 窗初就在
    w._last_seen["c1"] = _WIN_START + 3.0        # 第 3s 被 terminate，之后不再被观测到
    # 3s × 15fps ≈ 45 帧，出帧是满的
    w._stat_rendered["c1"] = 45
    w._stat_stale["c1"] = 45
    w._render_calls = 45
    w._render_time_sum = 45 * 0.002
    w._render_time_max = 0.005

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_not_called()


def test_snapshot_skips_verdict_for_too_short_span():
    """观测跨度 < 1s → 样本太短，只打数不下压力判定（不因它单独触发日志）。"""
    w = _worker(target_fps=30.0, output_fps=15.0)
    window = 10.0
    w._tick_count = 300
    w._first_seen["c1"] = _WIN_START + 9.7       # 只活了 0.3s
    w._last_seen["c1"] = _WIN_START + window
    w._stat_rendered["c1"] = 5
    w._stat_stale["c1"] = 3
    w._render_calls = 5
    w._render_time_sum = 5 * 0.002
    w._render_time_max = 0.004

    with patch("app.services.inference.visualization.worker.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_not_called()
