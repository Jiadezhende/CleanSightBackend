"""断流判定：`GAP_THRESHOLD_MS` / `first_gap` / `total_gap_ms`。

媒体轴本身（落点、选段、墙钟↔媒体换算、不带阈值的 `wall_gaps()`）的用例在
`tests/test_storage_hls.py`；这里只钉阈值这一条判断。
"""

import pytest

from app.services.utils.media_timeline import GAP_THRESHOLD_MS, first_gap, total_gap_ms
from app.storage import hls
from factories import make_run, seed_hls_segments

TASK_ID = 1
STEP_ID = 1
TS0 = 1_700_000_000_000            # 首段墙钟起点（epoch ms）


def _seed(items, **kw):
    return seed_hls_segments(TASK_ID, STEP_ID, items, **kw)


def _load(track="raw"):
    return hls.query_timeline(make_run(TASK_ID, STEP_ID), track)


def _contiguous(n: int, extinf_s: float = 10.0):
    """n 个首尾相接的段（墙钟间隔 = EXTINF，即无空洞）。"""
    step_ms = int(extinf_s * 1000)
    return [(TS0 + i * step_ms, extinf_s) for i in range(n)]


def _with_gap(gap_s: float, extinf_s: float = 10.0):
    """两段，中间隔着 `gap_s` 秒的空洞。"""
    second = TS0 + int((extinf_s + gap_s) * 1000)
    return [(TS0, extinf_s), (second, extinf_s)]


# ---------------------------------------------------------------------------
# 空洞判据
# ---------------------------------------------------------------------------


class TestGaps:
    """`gap = 下一段起点 − (本段起点 + EXTINF)`，阈值 `GAP_THRESHOLD_MS`。

    被它替换掉的旧判据拿「该 step 全量段相邻间隔的中位数」当基准、再留一个可配容差 ——
    那是**没有段时长真值**时的补偿（旧实现刻意不读清单 EXTINF）。
    """

    def test_systematic_fps_drift_has_no_gap(self, tmp_storage):
        """每段墙钟跨度都 >10s（真实 fps < raw_fps 的系统漂移）→ EXTINF 同步变长 → 无空洞。

        这是旧判据存在的全部理由，新判据**不需要为它做任何补偿**：漂移压低 `eff_fps`，
        `EXTINF = N/eff_fps` 随之变长，两边一起动。
        """
        items, ts = [], TS0
        for extinf in (10.6, 10.83, 10.7, 10.9):
            items.append((ts, extinf))
            ts += int(extinf * 1000)
        _seed(items)

        assert first_gap(_load()) is None

    @pytest.mark.parametrize("gap_ms", [0, 100, GAP_THRESHOLD_MS])
    def test_at_or_below_threshold_passes(self, tmp_storage, gap_ms):
        """帧间隔抖动让 gap 偏离 0 几百毫秒 —— 阈值以内放行。

        gap 的真身是「跨边界那一次帧间隔 − 段内平均帧间隔」，零均值但有尾。
        """
        _seed(_with_gap(gap_ms / 1000))
        assert first_gap(_load()) is None

    def test_just_over_threshold_is_a_gap(self, tmp_storage):
        """钉住边界，免得阈值被悄悄放大。"""
        _seed(_with_gap((GAP_THRESHOLD_MS + 100) / 1000))

        gap = first_gap(_load())

        assert gap is not None and gap[2] == GAP_THRESHOLD_MS + 100

    def test_reports_the_exact_gap_and_the_two_segments(self, tmp_storage):
        _seed(_with_gap(20.0))

        cur, nxt, gap_ms = first_gap(_load())

        assert gap_ms == 20_000
        assert cur.seg.ref.ts_ms == TS0
        assert nxt.seg.ref.ts_ms == TS0 + 30_000

    def test_total_gap_sums_every_hole(self, tmp_storage):
        _seed([
            (TS0, 10.0),
            (TS0 + 30_000, 10.0),        # 空洞 20s
            (TS0 + 45_000, 10.0),        # 空洞 5s
        ])

        assert total_gap_ms(_load()) == 25_000

    def test_total_gap_is_zero_when_contiguous(self, tmp_storage):
        """零断流必须算出 0。

        这条钉的是一个已经发生过的误报：前端曾用「墙钟跨度 − Σ EXTINF」去凑空洞总量，
        而 `/timeline` 的墙钟跨度取**双轨并集**、Σ EXTINF 是**单轨**的，两者不同尺——
        推理起步晚于取流时 processed 首段本就晚于 raw 首段，于是零断流也会算出假空洞。
        """
        _seed(_contiguous(4))
        assert total_gap_ms(_load()) == 0

    def test_total_gap_ignores_sub_threshold_jitter(self, tmp_storage):
        _seed(_with_gap(0.3))
        assert total_gap_ms(_load()) == 0

    def test_only_looks_inside_the_window(self, tmp_storage):
        """窗口之外的断流与这段区间的内容无关，不该牵连它。"""
        _seed([
            (TS0, 10.0),
            (TS0 + 10_000, 10.0),
            (TS0 + 40_000, 10.0),        # ← 与上一段之间有 20s 空洞
            (TS0 + 50_000, 10.0),
        ])
        tl = _load()

        assert first_gap(tl.select(0, 20_000)) is None        # 空洞之前
        assert first_gap(tl.select(20_000, 40_000)) is None   # 空洞之后
        assert first_gap(tl.select(0, 40_000)) is not None    # 跨过去才算
