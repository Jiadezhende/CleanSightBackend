"""
`step_store.hls` 单元测试 —— HLS 视频落盘域。

覆盖：
- segments：按 ts_us 升序、过滤非匹配文件、playable_only 两种语义
- list_steps：清单接口要的双轨段清单（一次扫描出全），只出有段的 step
- time_bounds_us：终点含末段 EXTINF、双轨并集、跳在途段
- vod_playlist：备料（滤在途、EXTINF 真值、TARGETDURATION）与三个领域异常的判定顺序
- write_segment：四件产物的原子提交与回滚、tfdt 前缀和、init 每轨一次、并发守卫
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


class TestWriteSegment:
    """落一段是一个原子单元：sidecar + 段 mp4 + init + playlist 的 EXTINF 行。"""

    @staticmethod
    def _put(task_id, step_id, track, start_ts, dur, *, frame_ts=None, body=b"seg"):
        """走完整事务落一段（字节由测试直接写，不起 ffmpeg）。"""
        with hls.write_segment(task_id, step_id, track, start_ts) as seg:
            seg.stage_path.write_bytes(body)
            if seg.init_stage_path is not None:
                seg.init_stage_path.write_bytes(b"init")
            seg.commit(duration_s=dur, frame_timestamps=frame_ts)
        return seg

    def test_commit_lands_all_four_artifacts(self, tmp_storage):
        self._put(1, 1, "raw", 1.5, 10.0, frame_ts=[1.5, 1.6])
        d = tmp_storage / "1" / "1"

        assert (d / "raw_segment_1500000.mp4").read_bytes() == b"seg"
        assert (d / "raw_segment_1500000.idx").exists()  # sidecar 与段同 stem
        assert (d / "raw_init.mp4").read_bytes() == b"init"
        assert (d / "raw_playlist.m3u8").read_text() == (
            "#EXTM3U\n"
            "#EXT-X-VERSION:7\n"
            "#EXT-X-TARGETDURATION:10\n"
            '#EXT-X-MAP:URI="raw_init.mp4"\n'
            "#EXTINF:10.000,\n"
            "raw_segment_1500000.mp4\n"
        )

    def test_ts_us_truncates(self, tmp_storage):
        """**截断而非四舍五入**：改成 round 会让「start_ts 恰为该段首帧」的定位无条件出错。"""
        self._put(1, 1, "raw", 0.9999999, 1.0)
        assert (tmp_storage / "1" / "1" / "raw_segment_999999.mp4").exists()

    def test_second_segment_appends_without_rewriting_header(self, tmp_storage):
        self._put(1, 1, "raw", 1.0, 10.0)
        self._put(1, 1, "raw", 11.0, 10.5)
        text = (tmp_storage / "1" / "1" / "raw_playlist.m3u8").read_text()

        assert text.count("#EXTM3U") == 1
        assert text.endswith("#EXTINF:10.500,\nraw_segment_11000000.mp4\n")

    def test_tfdt_offset_is_sum_of_prior_extinf(self, tmp_storage):
        """tfdt(N) = Σ EXTINF(0..N-1)。三套时间线（EXTINF / tfdt / 媒体时长）同源于此。"""
        with hls.write_segment(1, 1, "raw", 1.0) as seg:
            assert seg.tfdt_offset_s == 0.0  # 首段
            seg.stage_path.write_bytes(b"a")
            seg.init_stage_path.write_bytes(b"init")
            seg.commit(duration_s=10.0)

        with hls.write_segment(1, 1, "raw", 11.0) as seg:
            assert seg.tfdt_offset_s == pytest.approx(10.0)
            seg.stage_path.write_bytes(b"b")
            seg.commit(duration_s=10.0)

        with hls.write_segment(1, 1, "raw", 21.0) as seg:
            assert seg.tfdt_offset_s == pytest.approx(20.0)
            seg.stage_path.write_bytes(b"c")
            seg.commit(duration_s=10.0)

    def test_init_stage_is_offered_once_per_track(self, tmp_storage):
        """该轨已有 init 就不再收：同轨同编码参数下 SPS/PPS 一致，重复产出丢弃。"""
        with hls.write_segment(1, 1, "raw", 1.0) as seg:
            assert seg.init_stage_path is not None
            seg.stage_path.write_bytes(b"a")
            seg.init_stage_path.write_bytes(b"init")
            seg.commit(duration_s=10.0)

        with hls.write_segment(1, 1, "raw", 11.0) as seg:
            assert seg.init_stage_path is None
            seg.stage_path.write_bytes(b"b")
            seg.commit(duration_s=10.0)

        # 另一条轨仍要自己的 init：共用会变成「谁先转码谁定」，SPS/PPS 不匹配
        with hls.write_segment(1, 1, "processed", 1.0) as seg:
            assert seg.init_stage_path is not None
            seg.stage_path.write_bytes(b"p")
            seg.init_stage_path.write_bytes(b"pinit")
            seg.commit(duration_s=10.0)

        d = tmp_storage / "1" / "1"
        assert (d / "raw_init.mp4").read_bytes() == b"init"
        assert (d / "processed_init.mp4").read_bytes() == b"pinit"

    def test_only_raw_gets_a_sidecar(self, tmp_storage):
        """processed 是渲染结果、离线不消费，不产 sidecar。

        **写侧无条件传帧 ts**，产不产由布局决定——「哪条轨有 sidecar」不该让调用方判。
        """
        self._put(1, 1, "processed", 1.0, 10.0, frame_ts=[1.0, 1.1])
        d = tmp_storage / "1" / "1"
        assert (d / "processed_segment_1000000.mp4").exists()
        assert not (d / "processed_segment_1000000.idx").exists()

    def test_sidecar_is_readable_by_the_decoder(self, tmp_storage):
        """写侧 array('d') 与读侧 np.fromfile(float64) 必须逐字节等价 —— 破了就静默取错帧。"""
        import numpy as np

        ts = [1.5, 1.5666666, 1.6333333]
        self._put(1, 1, "raw", 1.5, 10.0, frame_ts=ts)
        got = np.fromfile(
            tmp_storage / "1" / "1" / "raw_segment_1500000.idx", dtype=np.float64
        )
        assert got.tolist() == ts  # 位级相等，不是近似

    def test_rolls_back_when_body_raises(self, tmp_storage):
        """异常 = 未提交：临时文件清掉，playlist 一个字节不动。"""
        self._put(1, 1, "raw", 1.0, 10.0)
        before = (tmp_storage / "1" / "1" / "raw_playlist.m3u8").read_text()

        with pytest.raises(RuntimeError):
            with hls.write_segment(1, 1, "raw", 11.0) as seg:
                seg.stage_path.write_bytes(b"half")
                raise RuntimeError("boom")

        d = tmp_storage / "1" / "1"
        assert (d / "raw_playlist.m3u8").read_text() == before
        assert not (d / "raw_segment_11000000.mp4").exists()
        assert list(d.glob(".stage_*")) == []  # 无残骸

    def test_rolls_back_when_never_committed(self, tmp_storage):
        """没抛异常但也没 commit（调用方自己判定该段作废）→ 同样不落盘。"""
        with hls.write_segment(1, 1, "raw", 1.0) as seg:
            seg.stage_path.write_bytes(b"unwanted")

        d = tmp_storage / "1" / "1"
        assert not (d / "raw_segment_1000000.mp4").exists()
        assert not (d / "raw_playlist.m3u8").exists()
        assert list(d.glob(".stage_*")) == []

    def test_rejects_invalid_track(self, tmp_storage):
        with pytest.raises(ValueError, match="Invalid track"):
            with hls.write_segment(1, 1, "bogus", 1.0):
                pass

    def test_stamps_activity(self, tmp_storage):
        """写入口必须刷 TTL 判据，否则该 step 静默过期被回收。"""
        assert store.last_activity_at(1, 1) is None
        with hls.write_segment(1, 1, "raw", 1.0):
            pass
        assert store.last_activity_at(1, 1) is not None

    def test_concurrent_write_on_same_track_raises(self, tmp_storage):
        """单写者是 tfdt 正确性的前提；被打破时要响亮失败，不能静默写坏偏移。"""
        with hls.write_segment(1, 1, "raw", 1.0):
            with pytest.raises(hls.HlsConcurrentWrite):
                with hls.write_segment(1, 1, "raw", 11.0):
                    pass

    def test_inflight_key_is_released_after_exception(self, tmp_storage):
        """守卫不能因为一次失败就把这条轨永久锁死。"""
        with pytest.raises(RuntimeError):
            with hls.write_segment(1, 1, "raw", 1.0):
                raise RuntimeError("boom")
        with hls.write_segment(1, 1, "raw", 1.0):
            pass  # 不抛即通过

    def test_other_track_is_not_blocked(self, tmp_storage):
        """守卫的粒度是 (step, track)：每条轨一份 playlist，各算各的累计 EXTINF。"""
        with hls.write_segment(1, 1, "raw", 1.0):
            with hls.write_segment(1, 1, "processed", 1.0):
                pass


class TestPlaylistEntries:
    def test_returns_pairs_in_write_order(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        _write_playlist(d, "raw", [1000, 2000], dur=9.5)
        assert hls.playlist_entries(1, 1, "raw") == [
            ("raw_segment_1000.mp4", 9.5),
            ("raw_segment_2000.mp4", 9.5),
        ]

    def test_empty_when_missing(self, tmp_storage):
        assert hls.playlist_entries(1, 1, "raw") == []


class TestSegmentRef:
    def test_ts_conversions(self):
        ref = SegmentRef(filename="raw_segment_1234567.mp4", ts_us=1_234_567)
        assert ref.ts_ms == 1234
        assert abs(ref.ts_s - 1.234567) < 1e-9

    def test_carries_only_what_the_caller_cannot_get_elsewhere(self):
        """task_id/step_id/track 都是调用方传进去问的，故不在值对象上。"""
        assert SegmentRef._fields == ("filename", "ts_us")
