"""`MediaTimeline`：段序列在媒体轴上的展开，以及墙钟↔媒体的双向换算。

被测的核心事实只有一条：**媒体轴是压紧的墙钟**。段间空隙在它上面不存在，于是
「首段墙钟 + 媒体刻度」这个换算只在从没断过流时成立，断过就偏早整整一个空洞。本文件
围绕它排：先钉落点怎么算，再钉两个方向的换算在跨空洞时各自给出什么，最后钉空洞判据。
"""

import pytest

from app.services.utils.media_timeline import GAP_THRESHOLD_MS, MediaTimeline
from app.storage import hls
from factories import seed_hls_segments

TASK_ID = 1
STEP_ID = 1
TS0 = 1_700_000_000_000_000        # 首段墙钟起点（us）
TS0_MS = TS0 // 1000


def _seed(items, **kw):
    return seed_hls_segments(TASK_ID, STEP_ID, items, **kw)


def _load(track="raw"):
    return MediaTimeline.load(TASK_ID, STEP_ID, track)


def _contiguous(n: int, extinf_s: float = 10.0):
    """n 个首尾相接的段（墙钟间隔 = EXTINF，即无空洞）。"""
    step_us = int(extinf_s * 1_000_000)
    return [(TS0 + i * step_us, extinf_s) for i in range(n)]


def _with_gap(gap_s: float, extinf_s: float = 10.0):
    """两段，中间隔着 `gap_s` 秒的空洞。"""
    second = TS0 + int((extinf_s + gap_s) * 1_000_000)
    return [(TS0, extinf_s), (second, extinf_s)]


# ---------------------------------------------------------------------------
# 展开：段从哪来、落点怎么算
# ---------------------------------------------------------------------------


class TestLoad:
    def test_media_starts_accumulate_extinf(self, tmp_storage):
        """段的媒体落点 = 此前所有 EXTINF 之和。

        这个值与另外两个是同一个数：写侧 hex-patch 进 fragment 的 `tfdt`（= 它 × 90000），
        以及 hls.js 解析清单后给出的 `fragment.start`。三者同源是整套换算的地基。
        """
        _seed([(TS0, 10.0), (TS0 + 10_000_000, 9.8), (TS0 + 19_800_000, 10.2)])

        tl = _load()

        assert [p.media_start_ms for p in tl] == [0, 10_000, 19_800]
        assert tl.duration_ms == 30_000

    def test_media_axis_is_compressed_across_a_gap(self, tmp_storage):
        """**媒体轴是压紧的**：断流 20s 之后，下一段的媒体起点仍然紧接上一段。

        这正是不能让前端自己换算墙钟的原因，也是空洞判据不能挪到媒体轴上的原因。
        """
        _seed(_with_gap(20.0))

        tl = _load()

        assert [p.media_start_ms for p in tl] == [0, 10_000]
        assert tl.duration_ms == 20_000

    def test_unregistered_segment_is_invisible(self, tmp_storage):
        """段文件在盘上、清单里没有 → 不是段。

        旧实现枚举文件系统，会把这种段喂给 ffmpeg，命中「exit 0 + 全日志级别无输出 +
        产物合法但少一截」那类静默失败（历史缺陷 #3）。
        """
        _seed([TS0])
        orphan = hls.segment_path(
            TASK_ID, STEP_ID, hls.SegmentRef(track="raw", ts_us=TS0 + 10_000_000)
        )
        orphan.write_bytes(b"not-registered")

        assert orphan.exists()                               # 盘上确实躺着它
        assert [p.seg.ref.ts_us for p in _load()] == [TS0]

    def test_tracks_are_independent(self, tmp_storage):
        """两轨各自独立切段，媒体轴也各是各的。"""
        _seed(_contiguous(3), track="raw")
        _seed([(TS0, 5.0)], track="processed")

        assert _load("raw").duration_ms == 30_000
        assert _load("processed").duration_ms == 5_000

    def test_missing_playlist_is_empty(self, tmp_storage):
        tl = _load()
        assert not tl
        assert len(tl) == 0
        assert tl.duration_ms == 0


# ---------------------------------------------------------------------------
# 选段
# ---------------------------------------------------------------------------


class TestSelect:
    def test_picks_only_the_overlapping_ones(self, tmp_storage):
        _seed(_contiguous(4))

        window = _load().select(15_000, 25_000)

        assert [p.media_start_ms for p in window] == [10_000, 20_000]

    def test_segment_end_comes_from_extinf(self, tmp_storage):
        """段尾 = 起点 + EXTINF。末段尤其——它没有"下一段"可以拿来推。"""
        _seed([(TS0, 4.0)])                                  # 只覆盖 [0, 4000) ms
        tl = _load()

        assert len(tl.select(3_900, 5_000)) == 1             # 尾巴还沾边
        assert len(tl.select(4_000, 5_000)) == 0             # 刚好出界

    def test_selected_window_keeps_absolute_coordinates(self, tmp_storage):
        """子集不重新归零 —— 否则调用方手上那个绝对刻度没法直接相减。"""
        _seed(_contiguous(3))

        window = _load().select(15_000, 25_000)

        assert window.media_offset_ms(15_000) == 5_000       # 相对窗口首段（媒体 10_000）


# ---------------------------------------------------------------------------
# 换算：媒体 → 墙钟
# ---------------------------------------------------------------------------


class TestWallFromMedia:
    def test_inside_a_segment_is_linear(self, tmp_storage):
        _seed(_contiguous(3))
        tl = _load()

        assert tl.wall_ms_at(0) == TS0_MS
        assert tl.wall_ms_at(12_345) == TS0_MS + 12_345

    def test_jumps_across_a_gap(self, tmp_storage):
        """媒体轴上相邻的两个刻度，墙钟上可以差一整个空洞 —— 换算必须逐段做。

        全局线性（`W0 + media_ms`）在这里会给出 +10_000 而不是 +30_000，差的正是那 20s。
        这就是缺陷 #1 的根：前端按全局线性上报，裁出来的 clip 整体早了 Σgap。
        """
        _seed(_with_gap(20.0))
        tl = _load()

        assert tl.wall_ms_at(9_999) == TS0_MS + 9_999
        assert tl.wall_ms_at(10_000) == TS0_MS + 30_000

    def test_beyond_the_end_clamps_to_the_last_segment_end(self, tmp_storage):
        _seed(_contiguous(2))

        assert _load().wall_ms_at(10**9) == TS0_MS + 20_000


# ---------------------------------------------------------------------------
# 换算：墙钟 → 媒体（上一节的逆，告警标记落点用它）
# ---------------------------------------------------------------------------


class TestMediaFromWall:
    def test_roundtrips_inside_a_segment(self, tmp_storage):
        _seed(_contiguous(3))
        tl = _load()

        for media_ms in (0, 7, 9_999, 10_000, 25_500):
            assert tl.media_ms_at(tl.wall_ms_at(media_ms)) == media_ms

    def test_roundtrips_across_a_gap(self, tmp_storage):
        """空洞两侧都要能往返 —— 告警标记正落在这些位置上。"""
        _seed(_with_gap(20.0))
        tl = _load()

        for media_ms in (0, 9_999, 10_000, 19_999):
            assert tl.media_ms_at(tl.wall_ms_at(media_ms)) == media_ms

    def test_wall_inside_a_gap_snaps_to_the_next_segment(self, tmp_storage):
        """空洞里的墙钟在媒体轴上没有对应刻度（宽度为零），吸附到下一段段首。

        吸到下一段而不是上一段段尾，是为了不声称"某一帧拍于空洞之中"。
        """
        _seed(_with_gap(20.0))
        tl = _load()

        assert tl.media_ms_at(TS0_MS + 15_000) == 10_000     # 空洞中段 → 第二段段首
        assert tl.media_ms_at(TS0_MS + 29_999) == 10_000     # 空洞末尾 → 同上

    def test_clamps_outside_the_track(self, tmp_storage):
        _seed(_contiguous(2))
        tl = _load()

        assert tl.media_ms_at(TS0_MS - 5_000) == 0           # 早于首段
        assert tl.media_ms_at(TS0_MS + 10**6) == 20_000      # 晚于末段

    def test_empty_timeline_is_zero(self, tmp_storage):
        assert _load().media_ms_at(TS0_MS) == 0


# ---------------------------------------------------------------------------
# 空洞判据
# ---------------------------------------------------------------------------


class TestGaps:
    """`gap = 下一段起点 − (本段起点 + EXTINF)`，阈值 `GAP_THRESHOLD_MS`。

    被它替换掉的旧判据拿「该 step 全量段相邻间隔的中位数」当基准、再留一个可配容差 ——
    那是**没有段时长真值**时的补偿（旧实现刻意不读清单 EXTINF）。
    """

    def test_contiguous_has_no_gap(self, tmp_storage):
        _seed(_contiguous(4))
        assert _load().first_gap() is None

    def test_systematic_fps_drift_has_no_gap(self, tmp_storage):
        """每段墙钟跨度都 >10s（真实 fps < raw_fps 的系统漂移）→ EXTINF 同步变长 → 无空洞。

        这是旧判据存在的全部理由，新判据**不需要为它做任何补偿**：漂移压低 `eff_fps`，
        `EXTINF = N/eff_fps` 随之变长，两边一起动。
        """
        items, ts = [], TS0
        for extinf in (10.6, 10.83, 10.7, 10.9):
            items.append((ts, extinf))
            ts += int(extinf * 1_000_000)
        _seed(items)

        assert _load().first_gap() is None

    @pytest.mark.parametrize("gap_ms", [0, 100, GAP_THRESHOLD_MS])
    def test_at_or_below_threshold_passes(self, tmp_storage, gap_ms):
        """帧间隔抖动让 gap 偏离 0 几百毫秒 —— 阈值以内放行。

        gap 的真身是「跨边界那一次帧间隔 − 段内平均帧间隔」，零均值但有尾。
        """
        _seed(_with_gap(gap_ms / 1000))
        assert _load().first_gap() is None

    def test_just_over_threshold_is_a_gap(self, tmp_storage):
        """钉住边界，免得阈值被悄悄放大。"""
        _seed(_with_gap((GAP_THRESHOLD_MS + 100) / 1000))

        gap = _load().first_gap()

        assert gap is not None and gap[2] == GAP_THRESHOLD_MS + 100

    def test_reports_the_exact_gap_and_the_two_segments(self, tmp_storage):
        _seed(_with_gap(20.0))

        cur, nxt, gap_ms = _load().first_gap()

        assert gap_ms == 20_000
        assert cur.seg.ref.ts_us == TS0
        assert nxt.seg.ref.ts_us == TS0 + 30_000_000

    def test_two_segments_are_enough_to_judge(self, tmp_storage):
        """**行为改善**：只有两段也判得出停顿。

        旧判据在这里必然放行 —— 单一间隔样本既是观测值又是基准，excess 恒为 0，区分不了
        「漂移」与「停顿」。EXTINF 给的是段自身的长度，不依赖样本量。
        """
        _seed(_with_gap(20.0))
        assert _load().first_gap() is not None

    def test_total_gap_sums_every_hole(self, tmp_storage):
        _seed([
            (TS0, 10.0),
            (TS0 + 30_000_000, 10.0),        # 空洞 20s
            (TS0 + 45_000_000, 10.0),        # 空洞 5s
        ])

        assert _load().total_gap_ms() == 25_000

    def test_total_gap_is_zero_when_contiguous(self, tmp_storage):
        """零断流必须算出 0。

        这条钉的是一个已经发生过的误报：前端曾用「墙钟跨度 − Σ EXTINF」去凑空洞总量，
        而 `/timeline` 的墙钟跨度取**双轨并集**、Σ EXTINF 是**单轨**的，两者不同尺——
        推理起步晚于取流时 processed 首段本就晚于 raw 首段，于是零断流也会算出假空洞。
        """
        _seed(_contiguous(4))
        assert _load().total_gap_ms() == 0

    def test_total_gap_ignores_sub_threshold_jitter(self, tmp_storage):
        _seed(_with_gap(0.3))
        assert _load().total_gap_ms() == 0

    def test_only_looks_inside_the_window(self, tmp_storage):
        """窗口之外的断流与这段区间的内容无关，不该牵连它。"""
        _seed([
            (TS0, 10.0),
            (TS0 + 10_000_000, 10.0),
            (TS0 + 40_000_000, 10.0),        # ← 与上一段之间有 20s 空洞
            (TS0 + 50_000_000, 10.0),
        ])
        tl = _load()

        assert tl.select(0, 20_000).first_gap() is None        # 空洞之前
        assert tl.select(20_000, 40_000).first_gap() is None   # 空洞之后
        assert tl.select(0, 40_000).first_gap() is not None    # 跨过去才算
