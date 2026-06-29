"""可视化吞吐量观测（[VIZ_THROUGHPUT]）测试。

覆盖 VisualizationWorker._log_throughput_snapshot 的"仅压力时打印"逻辑：
- 产出接近目标 fps → 静默（不刷屏）
- 产出明显低于目标 + 空转占比高 → 打印，标 supply-bound
- 单帧渲染耗时逼近 tick 预算 → 打印，标 render-bound
"""

import threading
from unittest.mock import patch

from app.services.inference.workers.visualization import VisualizationWorker


def _worker(target_fps: float = 20.0) -> VisualizationWorker:
    return VisualizationWorker(
        stop_event=threading.Event(),
        tick_interval=1.0 / target_fps,
    )


def test_snapshot_silent_when_healthy():
    """产出≈目标 fps、渲染快 → 不应打印 [VIZ_THROUGHPUT]。"""
    w = _worker(target_fps=20.0)
    window = 10.0
    # 10s 内渲染 200 帧 = 20fps，几乎不空转
    w._stat_rendered["c1"] = 200
    w._stat_stale["c1"] = 0
    w._render_calls = 200
    w._render_time_sum = 200 * 0.002  # 平均 2ms
    w._render_time_max = 0.004        # 峰值 4ms ≪ 50ms 预算

    with patch("app.services.inference.workers.visualization.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_not_called()


def test_snapshot_logs_supply_bound():
    """渲染很快但产出仅 ~10fps + 大量空转 → 打印并标 supply-bound。"""
    w = _worker(target_fps=20.0)
    window = 10.0
    # 渲染 100 帧 = 10fps；另有 100 次空转（有推理但无新结果）
    w._stat_rendered["c1"] = 100
    w._stat_stale["c1"] = 100
    w._render_calls = 100
    w._render_time_sum = 100 * 0.002  # 平均 2ms，渲染清白
    w._render_time_max = 0.005        # 峰值 5ms ≪ 50ms 预算

    with patch("app.services.inference.workers.visualization.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_called_once()
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "[VIZ_THROUGHPUT]" in rendered
    assert "out=10.0fps" in rendered
    assert "supply-bound" in rendered


def test_snapshot_logs_render_bound():
    """单帧渲染峰值 ≥ tick 预算 → 打印并标 render-bound。"""
    w = _worker(target_fps=20.0)
    window = 10.0
    # 产出低（渲染慢导致），且渲染峰值 60ms ≥ 50ms 预算
    w._stat_rendered["c1"] = 90
    w._stat_stale["c1"] = 10
    w._render_calls = 90
    w._render_time_sum = 90 * 0.045   # 平均 45ms
    w._render_time_max = 0.060        # 峰值 60ms ≥ 50ms 预算

    with patch("app.services.inference.workers.visualization.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_called_once()
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "[VIZ_THROUGHPUT]" in rendered
    assert "render-bound" in rendered


def test_snapshot_silent_when_idle_stream():
    """近空闲流（几乎没有推理流入）即便产出低也不应误报。"""
    w = _worker(target_fps=20.0)
    window = 10.0
    # 整窗只有零星几帧，total ≪ expected_ticks*0.3（=60）→ 视为空闲非压力
    w._stat_rendered["c1"] = 5
    w._stat_stale["c1"] = 3
    w._render_calls = 5
    w._render_time_sum = 5 * 0.002
    w._render_time_max = 0.004

    with patch("app.services.inference.workers.visualization.logger") as log:
        w._log_throughput_snapshot(window)

    log.info.assert_not_called()
