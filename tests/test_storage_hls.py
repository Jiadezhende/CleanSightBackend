"""`app.storage.hls`：`{step}/hls/` 的定位、编解码与写入事务。

全程用 `tmp_storage` fixture（conftest）把存储根指到临时目录，不碰真实 `database/`。

四类断言，按规范 §7.6 排：

1. **往返**（T1/T2）：段名 ↔ SegmentRef、sidecar 二进制 ↔ float64 数组、EXTINF 行 ↔
   累计时长。`ts_us` 的往返只在 **us 域**闭合（截断有损，读侧的 `bisect` 建立在它上面）。
2. **事务不变式**（T3）：`insert_segment` 的 stage/commit 顺序与失败作废，用假的
   编码器与转码器测——最该测的断言不能躲在需要 ffmpeg 的函数背后。
3. **落位**：产物只进 `hls/` 子目录，step 根下不留文件；stage 目录 commit 后即消失。
4. **端到端**（T4）：真 cv2 + 真 ffmpeg 跑一遍，只这一条依赖外部二进制，缺料时 skip。

并发（T5）本期无断言：串行调度另有统一基建，本域刻意不加锁（见 `_insert` docstring）。
"""

import json
import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.domain.frame import Frame
from app.settings import settings
from app.storage import hls
from app.storage.hls import _decode, _encode, _fmp4, _idx, _layout, _m3u8, _meta


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------


def _frames(count=15, start=1700.0, fps=15.0, size=(16, 16)):
    """一段等间隔帧。默认 15 帧 @15fps → eff_fps 恰 15.0、EXTINF 恰 1.000。"""
    height, width = size
    return [
        Frame(
            timestamp=start + i / fps,
            frame=np.zeros((height, width, 3), dtype=np.uint8),
        )
        for i in range(count)
    ]


def _hls_dir(root: Path, task_id=1, step_id=2) -> Path:
    return root / str(task_id) / str(step_id) / "hls"


def _box(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + typ + body


def _fragment_bytes(tfdt_value: int = 0, version: int = 1) -> bytes:
    """最小合法 fMP4 fragment：`moof/traf/tfdt` + 一个 mdat。

    手工造而不是让 ffmpeg 产，正是 T3 要的——事务不变式的断言不能依赖外部二进制。
    """
    if version == 1:
        tfdt_body = b"\x01\x00\x00\x00" + struct.pack(">Q", tfdt_value)
    else:
        tfdt_body = b"\x00\x00\x00\x00" + struct.pack(">I", tfdt_value)
    moof = _box(b"moof", _box(b"traf", _box(b"tfdt", tfdt_body)))
    return moof + _box(b"mdat", b"\x00" * 16)


def _read_tfdt(path: Path) -> int:
    """从 fragment 里读回 baseMediaDecodeTime（v1）。"""
    data = path.read_bytes()
    moof = _fmp4._find_box_path(data, 0, len(data), (b"moof",))
    traf = _fmp4._find_box_path(data, moof[0], moof[1], (b"traf",))
    tfdt = _fmp4._find_box_path(data, traf[0], traf[1], (b"tfdt",))
    return struct.unpack(">Q", data[tfdt[0] + 4 : tfdt[0] + 12])[0]


@pytest.fixture
def fake_pipeline(monkeypatch):
    """把 ① stage 那两步（cv2 编码、ffmpeg 转码）换成假的，只留事务骨架。

    返回一个记录器，测试可以用它改写"转码产出什么"或让某一步失败。
    """

    class Pipeline:
        def __init__(self):
            self.encode_error = None
            self.transcode_error = None
            self.tfdt_seed = 0

        def write_mp4v(self, path, frames, fps):
            if self.encode_error is not None:
                raise self.encode_error
            path.write_bytes(b"mp4v-source")

        def transcode(self, stage):
            if self.transcode_error is not None:
                raise self.transcode_error
            fragment = stage / "fragment_0.mp4"
            fragment.write_bytes(_fragment_bytes(self.tfdt_seed))
            init = stage / "init.mp4"
            init.write_bytes(b"fake-init")
            return fragment, init

    pipeline = Pipeline()
    monkeypatch.setattr(_encode, "write_mp4v", pipeline.write_mp4v)
    monkeypatch.setattr(_fmp4, "transcode", pipeline.transcode)
    return pipeline


# ---------------------------------------------------------------------------
# 定位与命名（T1 / T2 / L2）
# ---------------------------------------------------------------------------


class TestLayout:
    def test_segment_name_roundtrip(self):
        ref = _layout.SegmentRef(track="processed", ts_us=1_700_000_123_456)
        assert hls.parse_segment_name(_layout.segment_name(ref)) == ref

    def test_ts_roundtrip_closes_in_us_domain(self):
        """T2：往返在 us 域闭合，**不是**回到原始 float ts —— 截断是有意有损。"""
        ts = 1700.0000019
        ref = _layout.SegmentRef("raw", hls.ts_to_us(ts))
        assert hls.parse_segment_name(_layout.segment_name(ref)).ts_us == int(ts * 1e6)

    def test_ts_to_us_truncates_not_rounds(self):
        """进位会让读侧段级定位的 `bisect_right - 1` 落到前一段。"""
        assert hls.ts_to_us(1.9999999) == 1_999_999

    @pytest.mark.parametrize(
        "name",
        [
            "../raw_segment_1.mp4",          # 路径逃逸
            "/abs/raw_segment_1.mp4",        # 绝对路径
            "sub/raw_segment_1.mp4",         # 带目录
            "other_segment_1.mp4",           # 非法 track
            "raw_segment_abc.mp4",           # 非数字 ts
            "raw_segment_1.mp4.tmp",         # 半成品
            "raw_segment_1.idx",             # sidecar 不是段
            "raw_init.mp4",
            "raw_playlist.m3u8",
            ".stage_raw_1",
        ],
    )
    def test_parse_rejects_non_segment_names(self, name):
        """L2：外部输入先经 parse 转结构，路径由结构重建 —— 逃逸结构上不可能。"""
        assert hls.parse_segment_name(name) is None

    def test_all_paths_land_in_hls_subdir(self, tmp_storage):
        ref = _layout.SegmentRef("raw", 42)
        expected = _hls_dir(tmp_storage)
        for path in (
            hls.segment_path(1, 2, ref),
            hls.sidecar_path(1, 2, ref),
            hls.init_path(1, 2, "raw"),
            hls.playlist_path(1, 2, "raw"),
            _layout.metadata_path(1, 2),
            _layout.stage_dir(1, 2, ref),
        ):
            assert path.parent == expected

    def test_sidecar_is_segment_with_idx_suffix(self, tmp_storage):
        """读侧用 `with_suffix('.idx')` 找 sidecar，写侧不能另拼一套名字。"""
        ref = _layout.SegmentRef("raw", 42)
        assert hls.sidecar_path(1, 2, ref) == hls.segment_path(1, 2, ref).with_suffix(".idx")

    def test_tracks_have_separate_playlists_and_inits(self, tmp_storage):
        assert hls.playlist_path(1, 2, "raw") != hls.playlist_path(1, 2, "processed")
        assert hls.init_path(1, 2, "raw") != hls.init_path(1, 2, "processed")

    @pytest.mark.parametrize("track", ["RAW", "raw ", "detection", ""])
    def test_invalid_track_raises(self, tmp_storage, track):
        with pytest.raises(ValueError):
            hls.playlist_path(1, 2, track)

    def test_locating_does_not_touch_disk(self, tmp_storage):
        hls.segment_path(1, 2, _layout.SegmentRef("raw", 42))
        assert not (tmp_storage / "1").exists()

    def test_create_makes_only_the_hls_dir(self, tmp_storage):
        hls.segment_path(1, 2, _layout.SegmentRef("raw", 42), create=True)
        assert _hls_dir(tmp_storage).is_dir()
        assert [p.name for p in (tmp_storage / "1" / "2").iterdir()] == ["hls"]


class TestListSegments:
    def test_returns_ts_ascending(self, tmp_storage, fake_pipeline):
        """升序是返回值的契约 —— 读侧的段级 searchsorted 直接建立在它上面。"""
        for start in (1900.0, 1700.0, 1800.0):     # 刻意不按序写
            hls.insert_segment(1, 2, "raw", _frames(start=start))

        got = hls.list_segments(1, 2, "raw")
        assert [r.ts_us for r in got] == [1_700_000_000, 1_800_000_000, 1_900_000_000]

    def test_filters_by_track(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        hls.insert_segment(1, 2, "processed", _frames(start=1700.0))

        assert [r.track for r in hls.list_segments(1, 2, "raw")] == ["raw"]
        assert [r.track for r in hls.list_segments(1, 2, "processed")] == ["processed"]

    def test_skips_every_non_segment_neighbour(self, tmp_storage, fake_pipeline):
        """playlist / init / sidecar / stage 目录都住在同一个域目录里，一个都不许混进来。"""
        hls.insert_segment(1, 2, "raw", _frames())
        stage = _layout.stage_dir(1, 2, _layout.SegmentRef("raw", 1_700_000_000))
        stage.mkdir()

        assert len(hls.list_segments(1, 2, "raw")) == 1
        # 域目录里确实还躺着别的东西，不是因为目录空才通过
        assert len(list(_hls_dir(tmp_storage).iterdir())) > 1

    def test_missing_domain_dir_returns_empty(self, tmp_storage):
        assert hls.list_segments(1, 2, "raw") == []

    @pytest.mark.parametrize("track", ["RAW", "detection", ""])
    def test_invalid_track_raises(self, tmp_storage, track):
        with pytest.raises(ValueError):
            hls.list_segments(1, 2, track)


class TestInitNameCodec:
    """`init_name` ↔ `parse_init_name`（T1）—— L2 在 init 侧的执行形态。"""

    @pytest.mark.parametrize("track", ["raw", "processed"])
    def test_roundtrip(self, track):
        assert hls.parse_init_name(hls.init_name(track)) == track

    @pytest.mark.parametrize(
        "name",
        [
            "evil_init.mp4",          # ← endswith("init.mp4") 会放行它
            "../raw_init.mp4",        # ← 同上
            "/abs/raw_init.mp4",
            "raw_init.mp4.bak",
            "RAW_init.mp4",
            "raw_init.MP4",
            "raw_segment_1700.mp4",
            "raw_playlist.m3u8",
            "_init.mp4",
            "",
        ],
    )
    def test_rejects_everything_that_is_not_an_init_name(self, name):
        """前两条正是本 parser 存在的理由：`/media/init/{token}` 解出的 filename 过去只经
        `endswith("init.mp4")`，于是路径只能靠事后校验兜，而不是由结构重建。"""
        assert hls.parse_init_name(name) is None

    def test_init_path_is_rebuilt_from_parsed_track(self, tmp_storage):
        """L2 的完整链条：外部字符串 → parse → track → 路径由结构重建。"""
        track = hls.parse_init_name("processed_init.mp4")
        assert hls.init_path(1, 2, track).name == "processed_init.mp4"


# ---------------------------------------------------------------------------
# sidecar 二进制（T1）
# ---------------------------------------------------------------------------


class TestSidecarCodec:
    def test_roundtrip_is_bit_exact(self, tmp_path):
        """离线反查拿 sidecar 值与内存帧 ts 做**相等**比较，位级保真是契约。"""
        timestamps = [1700.0, 1700.0666666666667, 1700.1333333333334]
        path = tmp_path / "raw_segment_1.idx"
        _idx.write(path, timestamps)
        assert list(_idx.read(path)) == timestamps

    def test_layout_is_bare_float64_array(self, tmp_path):
        path = tmp_path / "raw_segment_1.idx"
        _idx.write(path, [1.0, 2.0, 3.0])
        assert path.stat().st_size == 3 * 8
        assert list(np.fromfile(path, dtype=np.float64)) == [1.0, 2.0, 3.0]

    def test_missing_file_reads_empty(self, tmp_path):
        assert len(_idx.read(tmp_path / "nope.idx")) == 0

    def test_write_leaves_no_tmp(self, tmp_path):
        _idx.write(tmp_path / "raw_segment_1.idx", [1.0])
        assert [p.name for p in tmp_path.iterdir()] == ["raw_segment_1.idx"]


# ---------------------------------------------------------------------------
# playlist 文本（T1）
# ---------------------------------------------------------------------------


class TestPlaylistCodec:
    def test_entry_and_total_duration_roundtrip(self, tmp_path):
        playlist = tmp_path / "raw_playlist.m3u8"
        for i, duration in enumerate([1.0, 2.5, 0.125]):
            _m3u8.append(playlist, "raw_init.mp4", duration, f"raw_segment_{i}.mp4")
        assert _m3u8.total_duration(playlist) == pytest.approx(3.625)

    def test_header_written_once_and_declares_init(self, tmp_path):
        playlist = tmp_path / "raw_playlist.m3u8"
        _m3u8.append(playlist, "raw_init.mp4", 1.0, "raw_segment_0.mp4")
        _m3u8.append(playlist, "raw_init.mp4", 1.0, "raw_segment_1.mp4")
        text = playlist.read_text(encoding="utf-8")
        assert text.count("#EXTM3U") == 1
        assert text.count('#EXT-X-MAP:URI="raw_init.mp4"') == 1
        assert text.count("#EXTINF:") == 2

    def test_no_endlist_in_live_playlist(self, tmp_path):
        """写侧维护的是 LIVE 清单，VOD 形态由读侧另生成。"""
        playlist = tmp_path / "raw_playlist.m3u8"
        _m3u8.append(playlist, "raw_init.mp4", 1.0, "raw_segment_0.mp4")
        assert "#EXT-X-ENDLIST" not in playlist.read_text(encoding="utf-8")

    def test_missing_playlist_totals_zero(self, tmp_path):
        """首段读不到任何条目 → tfdt 落点 0。"""
        assert _m3u8.total_duration(tmp_path / "nope.m3u8") == 0.0

    def test_corrupt_extinf_line_is_skipped(self, tmp_path):
        playlist = tmp_path / "raw_playlist.m3u8"
        playlist.write_text(
            "#EXTM3U\n#EXTINF:1.000,\na.mp4\n#EXTINF:nan-ish,\nb.mp4\n#EXTINF:2.000,\nc.mp4\n",
            encoding="utf-8",
        )
        assert _m3u8.total_duration(playlist) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# 读侧：逐段 EXTINF（T1，与写侧 append 互为逆运算）
# ---------------------------------------------------------------------------


class TestPlaylistDurations:
    def test_roundtrip_with_append(self, tmp_path):
        """写侧写进去的 EXTINF，读侧逐段读回；求和与 total_duration 同源。"""
        playlist = tmp_path / "raw_playlist.m3u8"
        written = {"raw_segment_0.mp4": 1.0, "raw_segment_1.mp4": 2.5, "raw_segment_2.mp4": 0.125}
        for name, duration in written.items():
            _m3u8.append(playlist, "raw_init.mp4", duration, name)

        got = _m3u8.durations(playlist)
        assert got == pytest.approx(written)
        assert sum(got.values()) == pytest.approx(_m3u8.total_duration(playlist))

    def test_missing_playlist_is_empty(self, tmp_path):
        assert _m3u8.durations(tmp_path / "nope.m3u8") == {}

    def test_header_only_playlist_is_empty(self, tmp_path):
        playlist = tmp_path / "raw_playlist.m3u8"
        playlist.write_text(_m3u8.header("raw_init.mp4"), encoding="utf-8")
        assert _m3u8.durations(playlist) == {}

    def test_corrupt_extinf_isolates_only_its_own_entry(self, tmp_path):
        """R6：内容坏了逐行隔离 —— 一条坏 EXTINF 不许带走它后面的段。"""
        playlist = tmp_path / "raw_playlist.m3u8"
        playlist.write_text(
            "#EXTM3U\n#EXTINF:1.000,\na.mp4\n#EXTINF:nan-ish,\nb.mp4\n#EXTINF:2.000,\nc.mp4\n",
            encoding="utf-8",
        )
        assert _m3u8.durations(playlist) == {"a.mp4": 1.0, "c.mp4": 2.0}


# ---------------------------------------------------------------------------
# 读侧产出①：段容器（可播过滤 / 双轨枚举 / 区间定位）
# ---------------------------------------------------------------------------


class TestPlayableSegments:
    def test_filters_segments_missing_from_playlist(self, tmp_storage, fake_pipeline):
        """判据是"在不在清单键集合里"，不是"盘上有没有这个文件"。

        造的是登记失败那一档：段文件已就位、清单里没它。旧平铺布局的在途段同理。
        """
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        orphan = _hls_dir(tmp_storage) / "raw_segment_9999999999.mp4"
        orphan.write_bytes(b"not-registered")

        assert len(hls.list_segments(1, 2, "raw")) == 2        # 盘上确实有两个
        got = hls.playable_segments(1, 2, "raw")
        assert [s.ref.ts_us for s in got] == [1_700_000_000]   # 能播的只有一个

    def test_returns_ts_ascending_with_durations(self, tmp_storage, fake_pipeline):
        for start in (1900.0, 1700.0, 1800.0):                 # 刻意不按序写
            hls.insert_segment(1, 2, "raw", _frames(start=start))

        got = hls.playable_segments(1, 2, "raw")
        assert [s.ref.ts_us for s in got] == [1_700_000_000, 1_800_000_000, 1_900_000_000]
        assert [s.duration_s for s in got] == pytest.approx([1.0, 1.0, 1.0])

    def test_tracks_do_not_cross(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        hls.insert_segment(1, 2, "processed", _frames(start=1800.0))

        assert [s.ref.track for s in hls.playable_segments(1, 2, "raw")] == ["raw"]
        assert [s.ref.ts_us for s in hls.playable_segments(1, 2, "processed")] == [1_800_000_000]

    def test_missing_domain_dir_is_empty(self, tmp_storage):
        assert hls.playable_segments(1, 2, "raw") == []

    def test_missing_playlist_is_empty(self, tmp_storage):
        """段文件在、清单不在 → 一个都不能播（不是"全都能播"）。"""
        target = _hls_dir(tmp_storage)
        target.mkdir(parents=True)
        (target / "raw_segment_1700000000.mp4").write_bytes(b"orphan")

        assert len(hls.list_segments(1, 2, "raw")) == 1
        assert hls.playable_segments(1, 2, "raw") == []

    @pytest.mark.parametrize("track", ["RAW", "detection", ""])
    def test_invalid_track_raises(self, tmp_storage, track):
        with pytest.raises(ValueError):
            hls.playable_segments(1, 2, track)


class TestSegmentsByTrack:
    def test_both_tracks_in_one_scan(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        hls.insert_segment(1, 2, "processed", _frames(start=1800.0))

        by_track = _layout.list_segments_by_track(1, 2)
        assert [r.ts_us for r in by_track["raw"]] == [1_700_000_000]
        assert [r.ts_us for r in by_track["processed"]] == [1_800_000_000]

    def test_missing_domain_dir_returns_empty_lists_not_empty_dict(self, tmp_storage):
        """调用方直接按 track 取，不该先判键。"""
        by_track = _layout.list_segments_by_track(1, 2)
        assert set(by_track) == set(hls.TRACKS)
        assert all(v == [] for v in by_track.values())

    def test_single_track_view_is_the_same_data(self, tmp_storage, fake_pipeline):
        for start in (1900.0, 1700.0):
            hls.insert_segment(1, 2, "raw", _frames(start=start))
        assert _layout.list_segments_by_track(1, 2)["raw"] == hls.list_segments(1, 2, "raw")


class TestStepSummaryRecipe:
    """域**不出** step 摘要类型（要完整摘要的只有一个消费方，准入判据 2「< 2 不进」）。

    但阶段 2 的 `routers/task.py` 要自己统计，这里把那段配方钉住：与现役
    `SegmentFinder.list_steps` 逐字段相等，说明迁过去是零行为变更。
    """

    @staticmethod
    def _summarise(task_id):
        """调用方侧的三行统计 —— 阶段 2 迁进 routers/task.py 的就是它。"""
        from app.storage import tasks as step_tasks

        out = []
        for step_id in step_tasks.steps(task_id):
            by_track = hls.list_segments_by_track(task_id, step_id)
            tracks = tuple(t for t in hls.TRACKS if by_track[t])
            if not tracks:                      # 建了目录没写成段 → 点开黑屏，不进清单
                continue
            all_ts = [r.ts_us for t in tracks for r in by_track[t]]
            out.append((step_id, tracks, min(all_ts), max(all_ts)))
        return out

    def test_matches_segment_finder_list_steps(self, tmp_storage):
        """两边读的布局不同（平铺 vs `{step}/hls/`），故把同一组段名同时铺到两处。"""
        from app.services.traceback.segment_finder import SegmentFinder

        layout = {
            2: ["raw_segment_1700000000.mp4", "raw_segment_1900000000.mp4",
                "processed_segment_1800000000.mp4"],
            3: ["processed_segment_2000000000.mp4"],
            5: [],                                    # 空 step，两边都该丢弃
        }
        for step_id, names in layout.items():
            flat = tmp_storage / "1" / str(step_id)
            domain = flat / "hls"
            domain.mkdir(parents=True)
            for name in names:
                (flat / name).write_bytes(b"x")
                (domain / name).write_bytes(b"x")

        legacy = [
            (s.step_id, s.tracks, s.first_ts_us, s.last_ts_us)
            for s in SegmentFinder(tmp_storage).list_steps(1)
        ]
        assert legacy == self._summarise(1)
        assert [row[0] for row in legacy] == [2, 3]   # 不是因为两边都空才相等

    def test_span_takes_union_of_both_tracks(self, tmp_storage, fake_pipeline):
        """两轨边界不一定对齐 —— 跨度是"有画面的时间范围"，不是任一单轨的播放范围。"""
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        hls.insert_segment(1, 2, "processed", _frames(start=1950.0))

        assert self._summarise(1) == [(2, ("raw", "processed"), 1_700_000_000, 1_950_000_000)]


class TestSelectSegments:
    """段级区间定位。判据是**段起始 ts**，不是段的覆盖区间（理由见函数 docstring）。

    VOD 渲染的用例已随 `render_vod` 移出本域，见 `tests/test_utils_vod_playlist.py`。
    """

    @staticmethod
    def _seed(count=3, first=1700.0, gap=1.0):
        """连续 count 段，段起始间隔 gap 秒。默认 1700 / 1701 / 1702。"""
        for i in range(count):
            hls.insert_segment(1, 2, "raw", _frames(start=first + i * gap))

    def test_no_bounds_returns_everything(self, tmp_storage, fake_pipeline):
        self._seed()
        assert hls.select_segments(1, 2, "raw") == hls.list_segments(1, 2, "raw")

    def test_picks_the_segment_containing_start(self, tmp_storage, fake_pipeline):
        """start_ts 落在第二段中间 → 从第二段开始，不是从第三段。"""
        self._seed()
        got = hls.select_segments(1, 2, "raw", start_ts=1701.5)
        assert [r.ts_us for r in got] == [1_701_000_000, 1_702_000_000]

    def test_start_exactly_at_first_frame_keeps_that_segment(self, tmp_storage, fake_pipeline):
        """段名 ts_us 是**截断**值，故 start_ts*1e6 > ts_us —— 用 'left' 会漏掉整段。

        这不是"大部分情况下对"，是无条件错：任何 ts 只要小数部分非零就踩。
        """
        self._seed(first=1700.0000019)               # 截断后段名是 1700000001
        got = hls.select_segments(1, 2, "raw", start_ts=1700.0000019)
        assert got[0].ts_us == 1_700_000_001         # 第一段还在

    def test_end_before_first_segment_is_empty(self, tmp_storage, fake_pipeline):
        """hi = -1 时刻意不 clamp 成 0：救成 0 会把空区间误判成命中第 0 段。"""
        self._seed()
        assert hls.select_segments(1, 2, "raw", end_ts=1699.0) == []

    def test_end_inside_a_segment_keeps_it(self, tmp_storage, fake_pipeline):
        self._seed()
        got = hls.select_segments(1, 2, "raw", end_ts=1701.5)
        assert [r.ts_us for r in got] == [1_700_000_000, 1_701_000_000]

    def test_window_inside_one_segment(self, tmp_storage, fake_pipeline):
        self._seed()
        got = hls.select_segments(1, 2, "raw", start_ts=1701.2, end_ts=1701.8)
        assert [r.ts_us for r in got] == [1_701_000_000]

    def test_missing_domain_dir_is_empty(self, tmp_storage):
        assert hls.select_segments(1, 2, "raw", start_ts=0.0, end_ts=1.0) == []

    @pytest.mark.parametrize("track", ["RAW", "detection", ""])
    def test_invalid_track_raises(self, tmp_storage, track):
        with pytest.raises(ValueError):
            hls.select_segments(1, 2, track)

    @pytest.mark.parametrize(
        "start_ts, end_ts",
        [
            (None, None), (1701.5, None), (None, 1701.5), (1700.5, 1702.5),
            (1699.0, 1699.5), (None, 1699.0), (1703.0, None), (1701.0, 1701.0),
        ],
    )
    def test_matches_numpy_searchsorted(self, tmp_storage, fake_pipeline, start_ts, end_ts):
        """stdlib `bisect` 与原实现的 `np.searchsorted` 逐值等价。

        `iter_frames` 的段级裁剪本来就是这段逻辑，提成公开函数时换了实现（本模块要保持
        stdlib-only）。这条钉住换实现没换行为 —— ts_us < 2^53 时 float64 精确表示整数，
        两者切点相同。
        """
        self._seed()
        refs = hls.list_segments(1, 2, "raw")
        starts = np.array([r.ts_us for r in refs], dtype=np.float64)

        lo = 0 if start_ts is None else max(
            0, int(np.searchsorted(starts, start_ts * 1e6, side="right")) - 1
        )
        hi = len(refs) - 1 if end_ts is None else int(
            np.searchsorted(starts, end_ts * 1e6, side="right")
        ) - 1
        expected = [] if lo > hi else refs[lo : hi + 1]

        assert hls.select_segments(1, 2, "raw", start_ts=start_ts, end_ts=end_ts) == expected


# ---------------------------------------------------------------------------
# metadata.json（路线 C）
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_first_record_initialises_both_tracks(self, tmp_path):
        path = tmp_path / "metadata.json"
        _meta.record_segment(path, task_id=1, step_id=2, track="raw", duration_s=1.5, timestamp=1700.0)
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["task_id"] == 1 and document["step_id"] == 2
        assert document["raw_segments"] == {
            "count": 1, "total_duration": 1.5,
            "first_timestamp": 1700.0, "last_timestamp": 1700.0,
        }
        assert document["processed_segments"]["count"] == 0
        assert document["end_time"] is None

    def test_records_accumulate(self, tmp_path):
        path = tmp_path / "metadata.json"
        for ts in (1700.0, 1710.0):
            _meta.record_segment(path, task_id=1, step_id=2, track="raw", duration_s=10.0, timestamp=ts)
        raw = json.loads(path.read_text(encoding="utf-8"))["raw_segments"]
        assert (raw["count"], raw["total_duration"]) == (2, 20.0)
        assert (raw["first_timestamp"], raw["last_timestamp"]) == (1700.0, 1710.0)

    def test_corrupt_document_is_rebuilt_not_fatal(self, tmp_path):
        """派生量坏了不该让整段视频陪葬 —— 重建 + warning。"""
        path = tmp_path / "metadata.json"
        path.write_text("{ not json", encoding="utf-8")
        _meta.record_segment(path, task_id=1, step_id=2, track="raw", duration_s=1.0, timestamp=1700.0)
        assert json.loads(path.read_text(encoding="utf-8"))["raw_segments"]["count"] == 1

    def test_write_leaves_no_tmp(self, tmp_path):
        path = tmp_path / "metadata.json"
        _meta.record_segment(path, task_id=1, step_id=2, track="raw", duration_s=1.0, timestamp=1700.0)
        assert [p.name for p in tmp_path.iterdir()] == ["metadata.json"]


# ---------------------------------------------------------------------------
# tfdt 改写（T3：不依赖外部工具）
# ---------------------------------------------------------------------------


class TestTfdtPatch:
    def test_patch_v1_writes_value(self, tmp_path):
        fragment = tmp_path / "fragment_0.mp4"
        fragment.write_bytes(_fragment_bytes(0))
        assert _fmp4.patch_tfdt(fragment, 900_000) is True
        assert _read_tfdt(fragment) == 900_000

    def test_patch_keeps_file_size(self, tmp_path):
        """纯 metadata 改写：box size 不变，mdat 不动。"""
        fragment = tmp_path / "fragment_0.mp4"
        fragment.write_bytes(_fragment_bytes(0))
        before = fragment.stat().st_size
        _fmp4.patch_tfdt(fragment, 12345)
        assert fragment.stat().st_size == before

    @pytest.mark.parametrize(
        "data",
        [
            b"not-an-mp4-at-all",
            _box(b"moof", b"\x00" * 8),                      # 有 moof 无 traf
            _box(b"moof", _box(b"traf", b"\x00" * 8)),       # 有 traf 无 tfdt
        ],
    )
    def test_patch_returns_false_on_unexpected_shape(self, tmp_path, data):
        """结构与预期不符是**内容事实**，返回 False 交调用方定夺，不抛。"""
        fragment = tmp_path / "fragment_0.mp4"
        fragment.write_bytes(data)
        assert _fmp4.patch_tfdt(fragment, 900_000) is False

    def test_patch_refuses_v0_overflow(self, tmp_path):
        """32 位放不下时宁可不改 —— 截断出来是个错位的落点，比不改更坏。"""
        fragment = tmp_path / "fragment_0.mp4"
        fragment.write_bytes(_fragment_bytes(0, version=0))
        assert _fmp4.patch_tfdt(fragment, 0xFFFFFFFF + 1) is False

    def test_seconds_to_ticks_uses_pinned_timescale(self):
        assert _fmp4.TIMESCALE == 90000
        assert _fmp4.seconds_to_ticks(10.013) == 901_170


# ---------------------------------------------------------------------------
# eff_fps（三个值的同源点）
# ---------------------------------------------------------------------------


class TestEffectiveFps:
    def test_reads_rate_from_timestamps(self):
        assert _encode.effective_fps(_frames(15, fps=15.0)) == pytest.approx(15.0)

    @pytest.mark.parametrize(
        "frames",
        [
            _frames(1),                                   # 单帧
            [Frame(timestamp=5.0, frame=np.zeros((2, 2, 3), np.uint8))] * 3,  # span=0
            _frames(3, fps=1000.0),                       # 带外（>60）
            _frames(3, fps=0.1),                          # 带外（<1）
        ],
    )
    def test_degenerate_segments_fall_back(self, frames):
        assert _encode.effective_fps(frames) == 15.0

    def test_duration_is_frames_over_fps_not_ts_span(self):
        """EXTINF 比首末帧跨度多一个帧间隔 —— 末帧自身的显示时长。"""
        frames = _frames(15, fps=15.0)
        fps = _encode.effective_fps(frames)
        span = frames[-1].timestamp - frames[0].timestamp
        assert _encode.media_duration(len(frames), fps) == pytest.approx(span + 1 / 15)


# ---------------------------------------------------------------------------
# insert_segment：事务不变式（T3）
# ---------------------------------------------------------------------------


class TestInsertSegment:
    def test_raw_insert_produces_the_full_set(self, tmp_storage, fake_pipeline):
        ref = hls.insert_segment(1, 2, "raw", _frames())
        assert ref == _layout.SegmentRef("raw", 1_700_000_000)

        names = sorted(p.name for p in _hls_dir(tmp_storage).iterdir())
        assert names == [
            "metadata.json",
            "raw_init.mp4",
            "raw_playlist.m3u8",
            "raw_segment_1700000000.idx",
            "raw_segment_1700000000.mp4",
        ]

    def test_step_root_holds_only_domain_dirs(self, tmp_storage, fake_pipeline):
        """域隔离的执行力：step 根下只有 `hls/`，没有文件。"""
        hls.insert_segment(1, 2, "raw", _frames())
        assert [p.name for p in (tmp_storage / "1" / "2").iterdir()] == ["hls"]

    def test_stage_dir_is_gone_after_commit(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames())
        assert not _layout.stage_dir(1, 2, _layout.SegmentRef("raw", 1_700_000_000)).exists()

    def test_processed_track_writes_no_sidecar(self, tmp_storage, fake_pipeline):
        """processed 是渲染结果、离线不消费 —— 这条不对称是有意的。"""
        hls.insert_segment(1, 2, "processed", _frames())
        assert not list(_hls_dir(tmp_storage).glob("*.idx"))

    def test_extinf_matches_frames_over_eff_fps(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames())
        text = hls.playlist_path(1, 2, "raw").read_text(encoding="utf-8")
        assert "#EXTINF:1.000,\nraw_segment_1700000000.mp4\n" in text

    def test_sidecar_holds_every_frame_ts(self, tmp_storage, fake_pipeline):
        frames = _frames()
        ref = hls.insert_segment(1, 2, "raw", frames)
        stored = _idx.read(hls.sidecar_path(1, 2, ref))
        assert list(stored) == [f.timestamp for f in frames]

    def test_second_segment_tfdt_equals_accumulated_extinf(self, tmp_storage, fake_pipeline):
        """tfdt(N) = Σ EXTINF(0..N-1) —— 媒体轴严丝合缝的全部理由。"""
        first = hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        second = hls.insert_segment(1, 2, "raw", _frames(start=1800.0))

        assert _read_tfdt(hls.segment_path(1, 2, first)) == 0
        assert _read_tfdt(hls.segment_path(1, 2, second)) == 90_000  # 1.000s × 90000

    def test_gap_in_wall_clock_leaves_no_gap_on_media_axis(self, tmp_storage, fake_pipeline):
        """墙钟上断流 100s，媒体轴上仍然接着放 —— 空隙只存在于文件名里。"""
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        hls.insert_segment(1, 2, "raw", _frames(start=1701.0))
        third = hls.insert_segment(1, 2, "raw", _frames(start=1801.0))
        assert _read_tfdt(hls.segment_path(1, 2, third)) == 180_000

    def test_init_written_once_per_track(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        init = hls.init_path(1, 2, "raw")
        init.write_bytes(b"first-init-kept")
        hls.insert_segment(1, 2, "raw", _frames(start=1701.0))
        assert init.read_bytes() == b"first-init-kept"

    def test_metadata_counts_the_segment(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames())
        hls.insert_segment(1, 2, "processed", _frames())
        document = json.loads(_layout.metadata_path(1, 2).read_text(encoding="utf-8"))
        assert document["raw_segments"]["count"] == 1
        assert document["processed_segments"]["count"] == 1

    # ── 入参 ────────────────────────────────────────────────────────────────

    def test_empty_frames_raises(self, tmp_storage, fake_pipeline):
        """空段不是"没事发生"，是调用方算错了批次。"""
        with pytest.raises(ValueError):
            hls.insert_segment(1, 2, "raw", [])

    def test_unknown_track_raises_before_touching_disk(self, tmp_storage, fake_pipeline):
        with pytest.raises(ValueError):
            hls.insert_segment(1, 2, "detection", _frames())
        assert not (tmp_storage / "1").exists()

    # ── 失败即整体作废（W4）────────────────────────────────────────────────

    @pytest.mark.parametrize("failing_step", ["encode", "transcode"])
    def test_stage_failure_publishes_nothing(self, tmp_storage, fake_pipeline, failing_step):
        setattr(fake_pipeline, f"{failing_step}_error", OSError("boom"))
        with pytest.raises(OSError):
            hls.insert_segment(1, 2, "raw", _frames())

        # 域目录可以存在（create 早于编码），但里面一个产物都不能有
        assert list(_hls_dir(tmp_storage).iterdir()) == []

    def test_tfdt_patch_failure_aborts_the_whole_segment(self, tmp_storage, fake_pipeline, monkeypatch):
        """tfdt 没修好的段进了清单 = 静默覆盖前段，宁可整段作废、让它喊出来。"""
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        monkeypatch.setattr(_fmp4, "patch_tfdt", lambda fragment, tick: False)

        with pytest.raises(RuntimeError):
            hls.insert_segment(1, 2, "raw", _frames(start=1800.0))

        names = sorted(p.name for p in _hls_dir(tmp_storage).iterdir())
        assert "raw_segment_1800000000.mp4" not in names
        assert "raw_segment_1800000000.idx" not in names   # 作废早于 sidecar 落盘
        text = hls.playlist_path(1, 2, "raw").read_text(encoding="utf-8")
        assert text.count("#EXTINF:") == 1                 # 前一段不受影响

    def test_failure_leaves_no_stage_dir(self, tmp_storage, fake_pipeline):
        fake_pipeline.transcode_error = OSError("boom")
        with pytest.raises(OSError):
            hls.insert_segment(1, 2, "raw", _frames())
        assert list(_hls_dir(tmp_storage).glob(".stage_*")) == []

    def test_retry_reuses_the_same_stage_key(self, tmp_storage, fake_pipeline):
        """W7：stage 名与产物同键，重试自然复用 —— 每段最多留一份残留。"""
        ref = _layout.SegmentRef("raw", 1_700_000_000)
        stage = _layout.stage_dir(1, 2, ref)
        stage.mkdir(parents=True)
        (stage / "leftover.bin").write_bytes(b"from a crashed run")

        assert hls.insert_segment(1, 2, "raw", _frames()) == ref
        assert not stage.exists()

    def test_sidecar_failure_does_not_sink_the_segment(self, tmp_storage, fake_pipeline, monkeypatch):
        """辅助索引写不进去，不该拿整段视频陪葬。"""
        def boom(path, timestamps):
            raise OSError("read-only")

        monkeypatch.setattr(_idx, "write", boom)
        ref = hls.insert_segment(1, 2, "raw", _frames())

        assert hls.segment_path(1, 2, ref).exists()
        assert not hls.sidecar_path(1, 2, ref).exists()
        assert "#EXTINF:" in hls.playlist_path(1, 2, "raw").read_text(encoding="utf-8")

    def test_tracks_do_not_collide(self, tmp_storage, fake_pipeline):
        """两轨各写各的清单与段名，同一时刻插两轨互不影响。"""
        raw = hls.insert_segment(1, 2, "raw", _frames())
        processed = hls.insert_segment(1, 2, "processed", _frames())
        assert raw.ts_us == processed.ts_us
        assert hls.segment_path(1, 2, raw) != hls.segment_path(1, 2, processed)
        for track in ("raw", "processed"):
            assert hls.playlist_path(1, 2, track).read_text(encoding="utf-8").count("#EXTINF:") == 1


# ---------------------------------------------------------------------------
# delete：域粒度删除（重启 supersede 的执行者）
# ---------------------------------------------------------------------------


class TestDelete:
    def test_removes_every_product_in_the_domain(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames())
        hls.insert_segment(1, 2, "processed", _frames())
        assert _hls_dir(tmp_storage).exists()

        assert hls.delete(1, 2) is True
        assert not _hls_dir(tmp_storage).exists()

    def test_does_not_touch_sibling_domains(self, tmp_storage, fake_pipeline):
        """只删本域 —— 同 step 的 features/ 一个字节都不碰，这正是域隔离换来的东西。"""
        hls.insert_segment(1, 2, "raw", _frames())
        features = tmp_storage / "1" / "2" / "features"
        features.mkdir(parents=True)
        (features / "features.jsonl").write_text("{}\n", encoding="utf-8")

        hls.delete(1, 2)

        assert (features / "features.jsonl").read_text(encoding="utf-8") == "{}\n"
        assert [p.name for p in (tmp_storage / "1" / "2").iterdir()] == ["features"]

    def test_does_not_touch_other_steps(self, tmp_storage, fake_pipeline):
        hls.insert_segment(1, 2, "raw", _frames())
        hls.insert_segment(1, 3, "raw", _frames())

        hls.delete(1, 2)

        assert not _hls_dir(tmp_storage, 1, 2).exists()
        assert list(_hls_dir(tmp_storage, 1, 3).glob("*.mp4"))

    def test_missing_domain_dir_returns_false(self, tmp_storage):
        assert hls.delete(1, 2) is False

    def test_next_insert_rebuilds_the_domain_dir(self, tmp_storage, fake_pipeline):
        """删完不用谁去重建：下一次 insert 的 create=True 自己会建。"""
        hls.insert_segment(1, 2, "raw", _frames(start=1700.0))
        hls.delete(1, 2)
        hls.insert_segment(1, 2, "raw", _frames(start=1800.0))

        text = hls.playlist_path(1, 2, "raw").read_text(encoding="utf-8")
        assert text.count("#EXTINF:") == 1                  # 上一代的条目没了
        assert "raw_segment_1800000000.mp4" in text


# ---------------------------------------------------------------------------
# 解码：两级裁剪与轨道契约（seam —— 不起 ffmpeg）
#
# 段级 + 帧级裁剪是纯 searchsorted 数学，把 `_run_ffmpeg` 这个唯一的解码 I/O 边界换成
# 「按 sidecar 合成帧」，就能不依赖 ffmpeg 覆盖全部边界情形。真实解码（ts ↔ 像素是否
# 错配）由下面的 TestDecodeEndToEnd 验，两者互补。
# ---------------------------------------------------------------------------


_DEC_FPS = 15.0
_DEC_PER_SEG = 10
_DEC_N_SEG = 4
_DEC_BASE = 1786731122.204701


def _dec_ts(gid: int) -> float:
    """全局帧号 → ts。与真实链路同款：**非等距**（带确定性抖动）。

    等距 ts 会让「按 ts 找帧号」退化成除法，掩盖 searchsorted 的边界错。
    """
    return _DEC_BASE + gid / _DEC_FPS + 0.004 * np.sin(gid * 1.7)


def _dec_frames(seg_index: int, size=(16, 16)):
    height, width = size
    return [
        Frame(
            timestamp=_dec_ts(seg_index * _DEC_PER_SEG + i),
            frame=np.zeros((height, width, 3), dtype=np.uint8),
        )
        for i in range(_DEC_PER_SEG)
    ]


@pytest.fixture
def decodable(tmp_storage, fake_pipeline):
    """4 段真 sidecar（段文件是假 fragment —— 解码边界被 seam 换掉，不碰那些字节）。"""
    for s in range(_DEC_N_SEG):
        hls.insert_segment(1, 2, "raw", _dec_frames(s))
    return tmp_storage


@pytest.fixture
def fake_decode(monkeypatch):
    """把解码 I/O 边界换成「按 sidecar 合成帧」—— 真实 `_run_ffmpeg` 的契约就是这个。

    返回调用记录 `(段名, k_start, k_end)`，用来验段级裁剪**确实只碰了该碰的段**：
    只断言产出的 ts 对，漏不掉「多起了几次 ffmpeg」这类只体现在耗时上的错。
    """
    calls = []

    def _fake(task_id, step_id, ref, sidecar, k_start, k_end, width, height):
        calls.append((_layout.segment_name(ref), k_start, k_end))
        for k in range(k_start, k_end + 1):
            yield Frame(
                timestamp=float(sidecar[k]),
                frame=np.zeros((height, width, 3), dtype=np.uint8),
            )

    monkeypatch.setattr(_decode, "_run_ffmpeg", _fake)
    return calls


def _ts_out(**kwargs):
    return [f.timestamp for f in hls.iter_frames(1, 2, width=2, height=2, **kwargs)]


class TestSegmentLevelTrim:
    def test_full_sweep_includes_last_segment(self, decodable, fake_decode):
        """默认区间必须含末段全部帧 —— 段起始数组的末元素是**末段段首**，
        拿它当时间轴末端会把末段砍到只剩第一帧。"""
        assert _ts_out() == [_dec_ts(g) for g in range(_DEC_N_SEG * _DEC_PER_SEG)]

    def test_start_inside_segment_keeps_that_segment(self, decodable, fake_decode):
        """起点落在段中部：包含它的那一段不能被跳过。"""
        g = _DEC_PER_SEG + 4
        assert _ts_out(start_ts=_dec_ts(g), end_ts=_dec_ts(g + 3)) == [
            _dec_ts(k) for k in range(g, g + 4)
        ]

    def test_start_exactly_on_segment_first_frame(self, decodable, fake_decode):
        """起点恰为段首帧：文件名 ts_us = int(ts*1e6) 截断 → start*1e6 > ts_us，
        用 side='left' 会连这一段一起跳过。"""
        g = 2 * _DEC_PER_SEG
        assert _ts_out(start_ts=_dec_ts(g), end_ts=_dec_ts(g + 2)) == [
            _dec_ts(k) for k in range(g, g + 3)
        ]

    def test_cross_segment_range_touches_only_needed_segments(self, decodable, fake_decode):
        g0, g1 = _DEC_PER_SEG + 6, 3 * _DEC_PER_SEG + 2
        assert _ts_out(start_ts=_dec_ts(g0), end_ts=_dec_ts(g1)) == [
            _dec_ts(k) for k in range(g0, g1 + 1)
        ]
        assert len(fake_decode) == 3            # 首段不入选，不该为它起解码

    def test_range_entirely_before_first_segment(self, decodable, fake_decode):
        """end_ts 早于首段起点 → hi = -1，不能被 clamp 成 0 后误出第 0 帧。"""
        assert _ts_out(start_ts=_DEC_BASE - 100, end_ts=_DEC_BASE - 50) == []
        assert fake_decode == []

    def test_range_entirely_after_last_frame(self, decodable, fake_decode):
        last = _dec_ts(_DEC_N_SEG * _DEC_PER_SEG - 1)
        assert _ts_out(start_ts=last + 50, end_ts=last + 100) == []

    def test_start_before_first_segment_clamps_to_head(self, decodable, fake_decode):
        assert _ts_out(start_ts=_DEC_BASE - 100, end_ts=_dec_ts(2)) == [
            _dec_ts(0), _dec_ts(1), _dec_ts(2)
        ]

    def test_no_segments_yields_nothing(self, tmp_storage, fake_decode):
        assert _ts_out() == []


class TestFrameLevelTrim:
    def _ref(self, seg_index: int) -> _layout.SegmentRef:
        return _layout.SegmentRef("raw", hls.ts_to_us(_dec_ts(seg_index * _DEC_PER_SEG)))

    def _seg_out(self, seg_index: int, **kwargs):
        frames = hls.read_segment(1, 2, self._ref(seg_index), width=2, height=2, **kwargs)
        return [f.timestamp for f in frames]

    def test_range_between_two_frames_is_empty(self, decodable, fake_decode):
        """区间完全落在两帧之间：k_start > k_end，不该"就近"给一帧。"""
        mid = (_dec_ts(3) + _dec_ts(4)) / 2
        lo, hi = mid - 1e-6, mid + 1e-6
        assert self._seg_out(0, start_ts=lo, end_ts=hi) == []

    def test_single_frame_range(self, decodable, fake_decode):
        assert self._seg_out(0, start_ts=_dec_ts(5), end_ts=_dec_ts(5)) == [_dec_ts(5)]

    def test_seam_adjacent_frames_across_segments(self, decodable, fake_decode):
        """跨段接缝的相邻两帧：一帧在前段末、一帧在后段首，都不能丢。"""
        g = _DEC_PER_SEG - 1
        assert _ts_out(start_ts=_dec_ts(g), end_ts=_dec_ts(g + 1)) == [
            _dec_ts(g), _dec_ts(g + 1)
        ]

    def test_missing_sidecar_skips_that_segment_only(self, decodable, fake_decode):
        """缺 .idx 跳过该段、不打断整条迭代 —— 一个辅助索引不该让前后所有段一起读不了。"""
        hls.sidecar_path(1, 2, self._ref(1)).unlink()

        got = _ts_out()

        assert got == [
            _dec_ts(g)
            for g in range(_DEC_N_SEG * _DEC_PER_SEG)
            if not (_DEC_PER_SEG <= g < 2 * _DEC_PER_SEG)
        ]
        assert len(fake_decode) == _DEC_N_SEG - 1   # 缺索引的那段根本没起解码


class TestDecodeTrackContract:
    def test_read_segment_rejects_processed(self, decodable, fake_decode):
        """processed 不落 sidecar 是有意的不对称：反查要的是原始帧。

        必须是 **ValueError 而不是静默零帧** —— 「这条路不通」与「这段没数据」是两回事。

        **刻意不 `list()`**：校验要在调用那一行就发生，不能潜伏到循环深处才炸。
        `read_segment` 自己不是生成器（只把 `_run_ffmpeg` 的生成器返回出去）正是为此，
        改成 `def ... yield` 会让这条红。
        """
        ref = _layout.SegmentRef("processed", hls.ts_to_us(_dec_ts(0)))
        with pytest.raises(ValueError, match="raw"):
            hls.read_segment(1, 2, ref, width=2, height=2)

    def test_iter_frames_has_no_track_parameter(self, decodable, fake_decode):
        """解码恒为 raw：给了 track 参数就得回答"processed 传进来怎么办"。"""
        with pytest.raises(TypeError):
            list(hls.iter_frames(1, 2, "raw", width=2, height=2))

    @pytest.mark.parametrize("missing", ["width", "height"])
    def test_resolution_has_no_default(self, decodable, fake_decode, missing):
        """规范设计约束 5：涉及正确性的参数不给默认值。漏传静默走 640×480 会在下游
        变成 train-serve skew，那是查不出来的那一档错。"""
        kwargs = {"width": 2, "height": 2}
        kwargs.pop(missing)
        with pytest.raises(TypeError):
            list(hls.iter_frames(1, 2, **kwargs))


class TestDecodeCommand:
    def test_keeps_the_frame_index_contract(self, tmp_storage):
        """段内帧号 = sidecar 下标，全靠这条命令的三个特征撑着。

        写错**不报错**：帧号原点一漂，反查回来的是错帧，而位级 ts 比较会把它当
        「没找到」抛 ValueError —— 错因指向完全错误的方向。
        """
        cmd = _decode._build_cmd(1, 2, _layout.SegmentRef("raw", 42), 3, 7, 64, 48)
        source = cmd[cmd.index("-i") + 1]

        assert "-ss" not in cmd                      # 按时间 seek 会让 n 的原点漂掉
        assert source.startswith("concat:")          # 不拼 init 解不开 fragment
        assert "raw_init.mp4" in source

        vf = cmd[cmd.index("-vf") + 1]
        # select 必须在 scale 之前：反过来会把注定被丢弃的帧也缩放一遍
        assert vf.index("select=") < vf.index("scale=")
        assert "between(n\\,3\\,7)" in vf            # 按帧号选，不是按时间

    def test_uses_the_raw_track_init(self, tmp_storage):
        """两轨各有各的 EXT-X-MAP，拿错 init 解出来的是另一条轨的画面。"""
        cmd = _decode._build_cmd(1, 2, _layout.SegmentRef("raw", 42), 0, 0, 8, 8)
        source = cmd[cmd.index("-i") + 1]

        assert str(hls.init_path(1, 2, "raw")) in source
        assert str(hls.init_path(1, 2, "processed")) not in source
        assert str(hls.segment_path(1, 2, _layout.SegmentRef("raw", 42))) in source


# ---------------------------------------------------------------------------
# 端到端（T4：唯一依赖外部二进制的一档）
# ---------------------------------------------------------------------------


def _external_tools_available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return Path(settings.ffmpeg_path).exists()


@pytest.mark.skipif(not _external_tools_available(), reason="需要 cv2 与项目自带 ffmpeg")
class TestInsertSegmentEndToEnd:
    """真 cv2 + 真 ffmpeg 跑两段，断言产物确实是 HLS 能放的东西。"""

    def test_two_real_segments(self, tmp_storage):
        first = hls.insert_segment(1, 2, "raw", _frames(start=1700.0, size=(64, 64)))
        second = hls.insert_segment(1, 2, "raw", _frames(start=1800.0, size=(64, 64)))

        # 是 fragment（styp + sidx + moof + mdat）不是自带 moov 的整块 mp4 ——
        # 后者喂给 hls.js 会 fragParsingError，这正是要转这一道的理由
        fragment = hls.segment_path(1, 2, second)
        head = fragment.read_bytes()
        assert head[4:8] == b"styp" and b"moof" in head
        assert hls.init_path(1, 2, "raw").read_bytes()[4:8] == b"ftyp"

        # 三条时间线对齐：tfdt(1) = EXTINF(0) × 90000，两段各 15 帧 @15fps → 各 1.000s
        playlist = hls.playlist_path(1, 2, "raw").read_text(encoding="utf-8")
        assert playlist.count("#EXTINF:1.000,") == 2
        assert _read_tfdt(hls.segment_path(1, 2, first)) == 0
        assert _read_tfdt(fragment) == _fmp4.seconds_to_ticks(1.0)

        # sidecar 与帧一一对应
        assert list(_idx.read(hls.sidecar_path(1, 2, first))) == [
            f.timestamp for f in _frames(start=1700.0)
        ]
        assert not list(_hls_dir(tmp_storage).glob(".stage_*"))

    def test_missing_ffmpeg_aborts_without_publishing(self, tmp_storage, monkeypatch):
        """D5：外部工具是运行时依赖 —— 缺二进制在写入时才炸，且什么都不留下。"""
        monkeypatch.setattr(settings, "ffmpeg_path", "definitely-not-ffmpeg")
        with pytest.raises((FileNotFoundError, OSError, subprocess.SubprocessError)):
            hls.insert_segment(1, 2, "raw", _frames(size=(64, 64)))
        assert list(_hls_dir(tmp_storage).iterdir()) == []


@pytest.mark.skipif(not _external_tools_available(), reason="需要 cv2 与项目自带 ffmpeg")
class TestDecodeEndToEnd:
    """真 cv2 编码 + 真 ffmpeg 转码/解码跑一遍 —— **段级往返闭合到内存对象**（规范 §7.8）。

    帧内中心色块编码 frame_id（三通道各 4 bit、每阶 17 阶距，抗两道有损压缩）。
    **这是唯一能抓「ts ↔ 像素错配」的手段**：只比 ts 的话，帧号整体平移一位照样全绿，
    而那正是解码命令写错时的表现。
    """

    SIZE = 64

    def _id_frame(self, gid: int) -> np.ndarray:
        """中心色块 = gid 的 12 bit（BGR 各 4 bit）。块够大、阶距够粗才扛得住压缩。"""
        img = np.zeros((self.SIZE, self.SIZE, 3), dtype=np.uint8)
        q = self.SIZE // 4
        img[q:-q, q:-q] = [
            (gid & 0xF) * 17,
            ((gid >> 4) & 0xF) * 17,
            ((gid >> 8) & 0xF) * 17,
        ]
        return img

    def _read_id(self, frame: np.ndarray) -> int:
        c = self.SIZE // 2
        b, g, r = frame[c - 4 : c + 4, c - 4 : c + 4].reshape(-1, 3).mean(axis=0)
        return (
            (int(round(float(b) / 17)) & 0xF)
            | ((int(round(float(g) / 17)) & 0xF) << 4)
            | ((int(round(float(r) / 17)) & 0xF) << 8)
        )

    def _write_segment(self, seg_index: int):
        frames = [
            Frame(
                timestamp=_dec_ts(seg_index * _DEC_PER_SEG + i),
                frame=self._id_frame(seg_index * _DEC_PER_SEG + i),
            )
            for i in range(_DEC_PER_SEG)
        ]
        return hls.insert_segment(1, 2, "raw", frames), frames

    def test_segment_roundtrip_is_frame_exact(self, tmp_storage):
        """`read_segment` 是 `insert_segment` 的逆运算：交出去的帧序列，原样回来。"""
        ref, written = self._write_segment(0)

        got = list(hls.read_segment(1, 2, ref, width=self.SIZE, height=self.SIZE))

        # ts **位级**相等（sidecar 存 float64 原值），不是近似 —— 离线反查按 `==` 配帧
        assert [f.timestamp for f in got] == [f.timestamp for f in written]
        # 像素与 ts 没有错位：第 k 帧的画面确实是第 k 帧的画面
        assert [self._read_id(f.frame) for f in got] == list(range(_DEC_PER_SEG))

    def test_iter_frames_spans_segments_in_order(self, tmp_storage):
        for s in range(2):
            self._write_segment(s)

        got = list(hls.iter_frames(1, 2, width=self.SIZE, height=self.SIZE))

        assert [self._read_id(f.frame) for f in got] == list(range(2 * _DEC_PER_SEG))
        assert [f.timestamp for f in got] == [_dec_ts(g) for g in range(2 * _DEC_PER_SEG)]

    def test_range_trim_lands_on_the_right_frames(self, tmp_storage):
        """帧级裁剪取的是**那几帧本身**，不是"附近几帧"。"""
        for s in range(2):
            self._write_segment(s)
        lo, hi = _DEC_PER_SEG - 2, _DEC_PER_SEG + 1   # 跨段接缝，两侧各两帧

        got = list(hls.iter_frames(
            1, 2, width=self.SIZE, height=self.SIZE,
            start_ts=_dec_ts(lo), end_ts=_dec_ts(hi),
        ))

        assert [self._read_id(f.frame) for f in got] == list(range(lo, hi + 1))

    def test_returned_frames_are_writable_and_independent(self, tmp_storage):
        """下游要对帧做 cv2 原地操作。`np.frombuffer` 出来的数组本身只读，且复用
        buffer 会就地改写调用方手里的上一帧 —— 两条都靠"逐帧新建 bytearray"兜住。"""
        ref, _ = self._write_segment(0)

        got = list(hls.read_segment(1, 2, ref, width=self.SIZE, height=self.SIZE))

        got[0].frame[0, 0, 0] = 255          # 只读的话这里 ValueError
        assert got[1].frame[0, 0, 0] != 255  # 共享 buffer 的话这里也会变成 255

    def test_processed_segment_is_refused_not_silently_empty(self, tmp_storage):
        """processed 真实落盘后照样拒绝 —— 它有段文件、没 sidecar，正是最容易
        被当成"这段没数据"的情形。"""
        ref = hls.insert_segment(1, 2, "processed", [
            Frame(timestamp=_dec_ts(i), frame=self._id_frame(i)) for i in range(_DEC_PER_SEG)
        ])
        assert hls.segment_path(1, 2, ref).exists()

        with pytest.raises(ValueError, match="raw"):
            hls.read_segment(1, 2, ref, width=self.SIZE, height=self.SIZE)
