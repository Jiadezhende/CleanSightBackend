"""
`step_store.hls` 单元测试 —— HLS 视频落盘域。

覆盖：
- segments：按 ts_us 升序、过滤非匹配文件、playable_only 两种语义
- list_steps：清单接口要的双轨段清单（一次扫描出全），只出有段的 step
- time_bounds_us：终点含末段 EXTINF、双轨并集、跳在途段
- vod_playlist：备料（滤在途、EXTINF 真值、TARGETDURATION）与三个领域异常的判定顺序
- 写路径：落盘名与 `_layout` 单一真源逐字一致、track 校验、顺带刷活动标记
- SegmentRef：单位换算与「只带调用方拿不到的东西」

目录契约那一层（枚举 / 文件出入口 / 活动标记 / traversal）在 test_step_store_dir.py。
"""

from pathlib import Path

import pytest

from app.services.step_store import hls, store
from app.services.step_store.hls import (
    HlsInitMissing,
    HlsNoPlayableSegments,
    HlsTrackMissing,
    SegmentRef,
)


def _make_step_dir(base: Path, task_id: int, step_id: int) -> Path:
    d = base / str(task_id) / str(step_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _touch_segment(step_dir: Path, track: str, ts_us: int) -> None:
    (step_dir / f"{track}_segment_{ts_us}.mp4").write_bytes(b"")


def _write_playlist(step_dir: Path, track: str, ts_us_list, dur: float = 10.0) -> None:
    """写 LIVE 形态 playlist（不含 ENDLIST），只收录 ts_us_list 里的段。

    不在其中的段即「在途段」—— 磁盘上有 mp4 但 transcode+append 未完成。
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f'#EXT-X-MAP:URI="{track}_init.mp4"']
    for ts in ts_us_list:
        lines += [f"#EXTINF:{dur:.3f},", f"{track}_segment_{ts}.mp4"]
    (step_dir / f"{track}_playlist.m3u8").write_text("\n".join(lines) + "\n")


class TestSegments:
    def test_returns_empty_when_dir_missing(self, tmp_storage):
        assert hls.segments(1, 1, "raw", playable_only=False) == []
        assert hls.segments(1, 1, "processed", playable_only=False) == []

    def test_lists_segments_sorted_ascending(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 100, 1)
        for ts in (3000, 1000, 2000):
            _touch_segment(d, "processed", ts)

        segs = hls.segments(100, 1, "processed", playable_only=False)
        assert [s.ts_us for s in segs] == [1000, 2000, 3000]

    def test_filters_by_track(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 2)
        _touch_segment(d, "raw", 100)
        _touch_segment(d, "raw", 200)
        _touch_segment(d, "processed", 100)

        assert {s.ts_us for s in hls.segments(1, 2, "raw", playable_only=False)} == {100, 200}
        assert {s.ts_us for s in hls.segments(1, 2, "processed", playable_only=False)} == {100}

    def test_step_id_isolation(self, tmp_storage):
        """同一 task 的不同 step 互不干扰。"""
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "processed", 1000)
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "processed", 2000)
        assert [s.ts_us for s in hls.segments(7, 1, "processed", playable_only=False)] == [1000]
        assert [s.ts_us for s in hls.segments(7, 2, "processed", playable_only=False)] == [2000]

    def test_ignores_non_matching_files(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        _touch_segment(d, "processed", 100)
        (d / "metadata.json").write_text("{}")
        (d / "raw_playlist.m3u8").write_text("")
        (d / "stray.json").write_text("[]")
        (d / "garbage.mp4").write_bytes(b"")

        segs = hls.segments(1, 1, "processed", playable_only=False)
        assert [s.filename for s in segs] == ["processed_segment_100.mp4"]

    def test_invalid_track_raises(self, tmp_storage):
        with pytest.raises(ValueError, match="Invalid track"):
            hls.segments(1, 1, "bogus")


class TestPlayableOnly:
    """在途段 = mp4 已落盘但不在 playlist 里（transcode+append 未完成）。"""

    def test_default_filters_in_flight_segments(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        _touch_segment(d, "raw", 1000)
        _touch_segment(d, "raw", 2000)
        _write_playlist(d, "raw", [1000])  # 2000 仍在途

        assert [s.ts_us for s in hls.segments(1, 1, "raw")] == [1000]
        assert [s.ts_us for s in hls.segments(1, 1, "raw", playable_only=False)] == [1000, 2000]

    def test_no_playlist_means_nothing_playable(self, tmp_storage):
        """整个 playlist 缺失（历史遗留 / 首段仍在 transcode）→ 无可播段。"""
        d = _make_step_dir(tmp_storage, 1, 1)
        _touch_segment(d, "raw", 1000)

        assert hls.segments(1, 1, "raw") == []
        assert len(hls.segments(1, 1, "raw", playable_only=False)) == 1


class TestListSteps:
    """清单接口要的形状：一次扫描出双轨段清单，不必再逐轨问盘。"""

    def test_reports_tracks_actually_on_disk(self, tmp_storage):
        # step 1 双轨，step 2 只有 raw —— 后者是大屏按 track 默认 processed 打 404 的成因
        d1 = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d1, "raw", 1000)
        _touch_segment(d1, "processed", 1000)
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "raw", 5000)

        steps = hls.list_steps(7)
        assert [s.step_id for s in steps] == [1, 2]
        # 顺序稳定为 ("raw", "processed")，对外 tracks 字段直接 list(by_track)
        assert list(steps[0].by_track) == ["raw", "processed"]
        assert list(steps[1].by_track) == ["raw"]

    def test_drops_step_dir_without_segments(self, tmp_storage):
        _make_step_dir(tmp_storage, 7, 1)  # 目录建了但没段（起流即失败）
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "raw", 1000)

        assert [s.step_id for s in hls.list_steps(7)] == [2]

    def test_drops_inference_only_step(self, tmp_storage):
        """只有 features.jsonl 的目录不进清单（点开是黑屏）；TTL 那边用 store.steps()。"""
        (_make_step_dir(tmp_storage, 7, 1) / "features.jsonl").write_text("{}")
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "raw", 1000)

        assert [s.step_id for s in hls.list_steps(7)] == [2]
        assert store.steps(7) == [(7, 1), (7, 2)]

    def test_carries_segments(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d, "raw", 1000)
        _touch_segment(d, "raw", 2000)

        (step,) = hls.list_steps(7)
        assert [s.ts_us for s in step.by_track["raw"]] == [1000, 2000]

    def test_playable_only_drops_step_with_only_in_flight_segments(self, tmp_storage):
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "raw", 1000)  # 无 playlist
        assert hls.list_steps(7) != []
        assert hls.list_steps(7, playable_only=True) == []

    def test_global_enumeration_spans_tasks(self, tmp_storage):
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "raw", 1000)
        _touch_segment(_make_step_dir(tmp_storage, 9, 3), "raw", 1000)

        assert [(s.task_id, s.step_id) for s in hls.list_steps()] == [(7, 1), (9, 3)]


class TestTimeBounds:
    def test_none_without_playlist(self, tmp_storage):
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "raw", 1_000_000)
        assert hls.time_bounds_us(7, 1) is None

    def test_end_includes_last_segment_extinf(self, tmp_storage):
        """终点取 max(ts + EXTINF)，不是 max(ts) —— 后者漏掉最后一段自身长度。"""
        d = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d, "raw", 1_000_000)
        _touch_segment(d, "raw", 11_000_000)
        _write_playlist(d, "raw", [1_000_000, 11_000_000], dur=10.0)

        assert hls.time_bounds_us(7, 1) == (1_000_000, 21_000_000)

    def test_spans_both_tracks(self, tmp_storage):
        """双轨取并集：两轨段边界不一定对齐。"""
        d = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d, "raw", 1_000_000)
        _touch_segment(d, "processed", 5_000_000)
        _write_playlist(d, "raw", [1_000_000], dur=2.0)
        _write_playlist(d, "processed", [5_000_000], dur=2.0)

        assert hls.time_bounds_us(7, 1) == (1_000_000, 7_000_000)

    def test_skips_in_flight_segments(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d, "raw", 1_000_000)
        _touch_segment(d, "raw", 99_000_000)  # 在途，无 EXTINF
        _write_playlist(d, "raw", [1_000_000], dur=3.0)

        assert hls.time_bounds_us(7, 1) == (1_000_000, 4_000_000)


class TestVodPlaylist:
    @pytest.fixture
    def step_dir(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        (d / "raw_init.mp4").write_bytes(b"init")
        for ts in (1_000_000, 11_000_000):
            _touch_segment(d, "raw", ts)
        _write_playlist(d, "raw", [1_000_000, 11_000_000], dur=10.4)
        return d

    def test_full_text(self, step_dir, tmp_storage):
        text = hls.vod_playlist(1, 1, "raw")
        assert text.splitlines() == [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            # RFC 8216 §4.3.2.1：判据是「EXTINF 四舍五入后 ≤ TARGETDURATION」，
            # 故 10.4 → 10（round），不是 ceil 的 11
            "#EXT-X-TARGETDURATION:10",
            "#EXT-X-MEDIA-SEQUENCE:0",
            '#EXT-X-MAP:URI="raw_init.mp4"',
            "#EXTINF:10.400,",
            "raw_segment_1000000.mp4",
            "#EXTINF:10.400,",
            "raw_segment_11000000.mp4",
            # 缺 ENDLIST → 播放端当直播流只读 live edge，前面的段全丢
            "#EXT-X-ENDLIST",
        ]

    def test_encode_uri_injects_token_form(self, step_dir, tmp_storage):
        text = hls.vod_playlist(
            1, 1, "raw", encode_uri=lambda kind, name: f"https://x/{kind}/{name}"
        )
        assert '#EXT-X-MAP:URI="https://x/init/raw_init.mp4"' in text
        assert "https://x/segment/raw_segment_1000000.mp4" in text

    def test_filters_in_flight_from_explicit_segments(self, step_dir, tmp_storage):
        """调用方给的段也要再滤一道 —— 它可能来自不滤在途段的查询。"""
        _touch_segment(step_dir, "raw", 21_000_000)  # 在途
        text = hls.vod_playlist(
            1, 1, "raw", segments=hls.segments(1, 1, "raw", playable_only=False)
        )
        assert "raw_segment_21000000.mp4" not in text

    def test_track_with_no_segments_raises_first(self, tmp_storage):
        """判定顺序：没录过这条轨（404）要早于缺 init（503）。"""
        _make_step_dir(tmp_storage, 1, 1)
        with pytest.raises(HlsTrackMissing, match="No processed segments"):
            hls.vod_playlist(1, 1, "processed")

    def test_missing_init_raises(self, step_dir, tmp_storage):
        (step_dir / "raw_init.mp4").unlink()
        with pytest.raises(HlsInitMissing, match="raw_init.mp4"):
            hls.vod_playlist(1, 1, "raw")

    def test_all_in_flight_raises(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        (d / "raw_init.mp4").write_bytes(b"init")
        _touch_segment(d, "raw", 1_000_000)
        with pytest.raises(HlsNoPlayableSegments):
            hls.vod_playlist(1, 1, "raw")

    def test_target_duration_floor_is_one(self, tmp_storage):
        """亚秒段（退化段）不能声明 TARGETDURATION:0。"""
        d = _make_step_dir(tmp_storage, 1, 1)
        (d / "raw_init.mp4").write_bytes(b"init")
        _touch_segment(d, "raw", 1_000)
        _write_playlist(d, "raw", [1_000], dur=0.2)
        assert "#EXT-X-TARGETDURATION:1" in hls.vod_playlist(1, 1, "raw")


class TestHasInit:
    def test_false_when_missing(self, tmp_storage):
        assert hls.has_init(1, 1, "raw") is False

    def test_true_when_present(self, tmp_storage):
        (_make_step_dir(tmp_storage, 1, 1) / "raw_init.mp4").write_bytes(b"init")
        assert hls.has_init(1, 1, "raw") is True

    def test_is_per_track(self, tmp_storage):
        """两轨各有各的 init —— 共用会变成「谁先转码谁定」，SPS/PPS 不匹配。"""
        (_make_step_dir(tmp_storage, 1, 1) / "raw_init.mp4").write_bytes(b"init")
        assert hls.has_init(1, 1, "processed") is False


class TestWritePaths:
    """落盘名与 `_layout` 单一真源逐字一致；写路径顺带刷活动标记。"""

    def test_names(self, tmp_storage):
        assert hls.segment_path(1, 1, "raw", 42).name == "raw_segment_42.mp4"
        assert hls.sidecar_path(1, 1, "raw", 42).name == "raw_segment_42.idx"
        assert hls.init_path(1, 1, "processed").name == "processed_init.mp4"
        assert hls.playlist_path(1, 1, "raw").name == "raw_playlist.m3u8"
        assert hls.metadata_path(1, 1).name == "metadata.json"
        assert hls.segment_path(1, 1, "raw", 42).parent == tmp_storage / "1" / "1"

    def test_rejects_invalid_track(self, tmp_storage):
        with pytest.raises(ValueError, match="Invalid track"):
            hls.segment_path(1, 1, "bogus", 42)

    def test_write_paths_stamp_activity(self, tmp_storage):
        """漏刷的表现是该 step 静默过期被回收——静默错误，必须锁住。"""
        writes = [
            lambda t, s: hls.segment_path(t, s, "raw", 1),
            lambda t, s: hls.sidecar_path(t, s, "raw", 1),
            lambda t, s: hls.init_path(t, s, "raw"),
            lambda t, s: hls.playlist_path(t, s, "raw"),
            lambda t, s: hls.metadata_path(t, s),
        ]
        for i, write in enumerate(writes):
            assert store.last_activity_at(3, i) is None
            write(3, i)
            assert store.last_activity_at(3, i) is not None, write

    def test_ts_to_us_truncates(self, tmp_storage):
        """**截断而非四舍五入**：改成 round 会让「start_ts 恰为该段首帧」的定位无条件出错。"""
        assert hls.ts_to_us(1.9999999) == 1_999_999
        assert hls.segment_path(1, 1, "raw", hls.ts_to_us(0.9999999)).name == (
            "raw_segment_999999.mp4"
        )


class TestSegmentRef:
    def test_ts_conversions(self):
        ref = SegmentRef(filename="raw_segment_1234567.mp4", ts_us=1_234_567)
        assert ref.ts_ms == 1234
        assert abs(ref.ts_s - 1.234567) < 1e-9

    def test_carries_only_what_the_caller_cannot_get_elsewhere(self):
        """task_id/step_id/track 都是调用方传进去问的，故不在值对象上。"""
        assert SegmentRef._fields == ("filename", "ts_us")
