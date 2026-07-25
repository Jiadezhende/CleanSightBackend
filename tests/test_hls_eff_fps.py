"""Fix A：processed/raw 段按实测 eff_fps 编码 —— HLSPersistenceStrategy._effective_fps 单测。

eff_fps = (N-1) / (ts_last - ts_first)；VideoWriter 与 EXTINF 同源，回放才对齐墙钟。
覆盖：正常反推、span<=0 / 单帧回退、带外（异常时间戳）回退。反推是全程自适应、不引用任何
上游 fps；无可测速率的退化段回退本地常量 `_DEGENERATE_FALLBACK_FPS`。
"""

from factories import make_frame
from app.services.persistence.strategies.hls_strategy import (
    HLSPersistenceStrategy,
    _DEGENERATE_FALLBACK_FPS,
)


def _frames(timestamps):
    return [make_frame(ts=ts) for ts in timestamps]


def test_effective_fps_normal():
    """300 帧均匀分布在 20s 跨度 → (300-1)/20 ≈ 14.95fps。"""
    n, span = 300, 20.0
    ts = [i * span / (n - 1) for i in range(n)]
    eff = HLSPersistenceStrategy._effective_fps(_frames(ts))
    assert eff == (n - 1) / span
    # segment_duration 用同一 eff_fps → 等于真实墙钟跨度
    assert abs(n / eff - span * n / (n - 1)) < 1e-6


def test_effective_fps_matches_wallclock_round_trip():
    """eff_fps 编码 N 帧的媒体时长 ≈ 真实跨度（差一帧），逐段播成 1.0x。"""
    ts = [10.0 + i * 0.09 for i in range(101)]  # 101 帧、跨度 9.0s → ~11.1fps
    eff = HLSPersistenceStrategy._effective_fps(_frames(ts))
    assert abs(eff - 100 / 9.0) < 1e-9


def test_effective_fps_single_frame_falls_back():
    eff = HLSPersistenceStrategy._effective_fps(_frames([5.0]))
    assert eff == _DEGENERATE_FALLBACK_FPS


def test_effective_fps_zero_span_falls_back():
    """所有帧同一时间戳（span=0）→ 回退退化兜底。"""
    eff = HLSPersistenceStrategy._effective_fps(_frames([7.0, 7.0, 7.0]))
    assert eff == _DEGENERATE_FALLBACK_FPS


def test_effective_fps_out_of_band_falls_back():
    """时间戳过于紧凑致反推值 > 60（异常）→ 回退退化兜底，避免慢放。"""
    ts = [0.0, 0.001, 0.002]  # (3-1)/0.002 = 1000fps，带外
    eff = HLSPersistenceStrategy._effective_fps(_frames(ts))
    assert eff == _DEGENERATE_FALLBACK_FPS


def test_effective_fps_negative_span_falls_back():
    """乱序时间戳致 span<0 → 回退退化兜底。"""
    eff = HLSPersistenceStrategy._effective_fps(_frames([10.0, 9.0]))
    assert eff == _DEGENERATE_FALLBACK_FPS
