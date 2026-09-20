"""`app.services.utils.vod_playlist`：VOD 形态 m3u8 的文本渲染。

纯函数，无落盘、无 fixture —— 除了最后那条与现役实现的逐字节比对，它要真造一组段。

三类断言：

1. **三种 URI 形态**都照渲染不问（裸文件名喂 ffmpeg / token 化 URL 给浏览器 / 绝对路径）
   ——"段 URI 长什么样"是调用方的判断，本模块不生成也不改写。
2. **硬格式约束**：ENDLIST 必在（缺了 ffmpeg 当直播流只读 live edge）、`map_uri` 必填
   （漏传该是 TypeError，不是运行时才炸）、`TARGETDURATION` 用 ceil（round 会违反 RFC 8216）。
3. **与现役 `StepExporter._build_vod_text` 逐字节相等** —— 阶段 2 迁移是零行为变更的唯一
   证据。这条随 `render_vod` 从 `tests/test_storage_hls.py` 一起搬过来，不能在搬家中丢。
"""

from pathlib import Path

import pytest

from app.services.utils.vod_playlist import VodEntry, render_vod
from app.storage import hls
from app.storage.hls import _layout, _m3u8


def _hls_dir(root: Path, task_id=1, step_id=2) -> Path:
    return root / str(task_id) / str(step_id) / "hls"


def _seed_segments(task_id, step_id, track, ts_list, duration_s=1.0):
    """铺一组已登记的段：段文件 + 用**写侧真函数**追出来的 LIVE 清单。

    不走 `insert_segment`：本文件测的是文本渲染，不该为此拖上 cv2 与 ffmpeg（或一个假
    编解码 fixture）。但清单必须由 `_m3u8.append` 真写——EXTINF 的落盘格式（三位小数、
    逗号后无标题）正是被比对的东西之一，手写清单会让比对测了个寂寞。
    """
    for ts_us in ts_list:
        ref = hls.SegmentRef(track=track, ts_us=ts_us)
        path = _layout.segment_path(task_id, step_id, ref, create=True)
        path.write_bytes(b"fake-fragment")
        _m3u8.append(
            _layout.playlist_path(task_id, step_id, track),
            _layout.init_name(track),
            duration_s,
            _layout.segment_name(ref),
        )


def _bare_entries(task_id, step_id, track):
    """调用方侧的装配：段 → VodEntry，URI 取裸文件名（喂 ffmpeg 的形态）。

    这三行就是阶段 2 迁移时每个调用点要自己写的那一份 —— 映射必须在调用方这一侧做，
    因为只有它知道 URI 该长什么样。
    """
    return [
        VodEntry(hls.segment_name(s.ref), s.duration_s)
        for s in hls.list_playable_segments(task_id, step_id, track)
    ]


class TestRenderVod:
    def test_bare_filename_form(self):
        """喂 ffmpeg 的形态：相对 URI 解析到同目录的 init 与各段。"""
        entries = [VodEntry("raw_segment_0.mp4", 1.0), VodEntry("raw_segment_1.mp4", 2.0)]
        assert render_vod(entries, map_uri="raw_init.mp4").splitlines() == [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-TARGETDURATION:2",
            "#EXT-X-MEDIA-SEQUENCE:0",
            '#EXT-X-MAP:URI="raw_init.mp4"',
            "#EXTINF:1.000,",
            "raw_segment_0.mp4",
            "#EXTINF:2.000,",
            "raw_segment_1.mp4",
            "#EXT-X-ENDLIST",
        ]

    def test_token_url_form(self):
        """浏览器回放的形态：URI 由服务层签好交进来，骨架一模一样。"""
        lines = render_vod(
            [VodEntry("http://h/media/segment/tok-a", 1.0)],
            map_uri="http://h/media/init/tok-i",
        ).splitlines()
        assert lines[5] == '#EXT-X-MAP:URI="http://h/media/init/tok-i"'
        assert lines[7] == "http://h/media/segment/tok-a"

    def test_absolute_path_form(self):
        """异地临时 m3u8 的形态：URI 是绝对路径，照渲染不问。"""
        lines = render_vod(
            [VodEntry("/srv/db/1/2/hls/raw_segment_0.mp4", 1.0)],
            map_uri="/srv/db/1/2/hls/raw_init.mp4",
        ).splitlines()
        assert lines[5] == '#EXT-X-MAP:URI="/srv/db/1/2/hls/raw_init.mp4"'
        assert lines[7] == "/srv/db/1/2/hls/raw_segment_0.mp4"

    def test_endlist_is_always_present(self):
        """缺 ENDLIST → ffmpeg 当直播流只读 live edge，前面的段全丢。"""
        out = render_vod([VodEntry("a.mp4", 1.0)], map_uri="raw_init.mp4")
        assert out.endswith("#EXT-X-ENDLIST\n")

    def test_empty_entries_raises(self):
        with pytest.raises(ValueError):
            render_vod([], map_uri="raw_init.mp4")

    def test_map_uri_is_required_keyword(self):
        """漏传该是 TypeError：fMP4 没有 EXT-X-MAP 解不出 codec init，而那是运行时才炸。"""
        with pytest.raises(TypeError):
            render_vod([VodEntry("a.mp4", 1.0)])

    @pytest.mark.parametrize(
        "longest, expected",
        [
            (10.0, 10),     # 整秒不进位
            (10.4, 11),     # ← round 会写出 10 而违反 RFC 8216（EXTINF 必须 ≤ TARGETDURATION）
            (10.001, 11),
            (0.2, 1),       # 下界
        ],
    )
    def test_target_duration_is_ceil_with_floor_one(self, longest, expected):
        entries = [VodEntry("a.mp4", 0.5), VodEntry("b.mp4", longest)]
        assert f"#EXT-X-TARGETDURATION:{expected}" in render_vod(entries, map_uri="raw_init.mp4")


class TestVodParityWithStepExporter:
    def test_byte_identical_to_current_implementation(self, tmp_storage):
        """与现役 `StepExporter._build_vod_text` 逐字节比对。

        它是三处 VOD 构造里唯一可直接调用的（另两处要 `Request` 或内联在 `_run_ffmpeg`
        里）。完全相等意味着阶段 2 迁移 step_exporter 是**零行为变更**。
        """
        from app.services.lab.step_exporter import StepExporter
        from app.services.traceback.segment_finder import SegmentRef as LegacyRef

        _seed_segments(1, 2, "raw", [1_700_000_000, 1_800_000_000, 1_900_000_000])

        durations = _m3u8.durations(hls.playlist_path(1, 2, "raw"))
        legacy_segs = [
            LegacyRef(
                task_id=1,
                step_id=2,
                track="raw",
                filename=name,
                ts_us=int(name.split("_")[-1].removesuffix(".mp4")),
                path=_hls_dir(tmp_storage) / name,
            )
            for name in sorted(durations)
        ]
        legacy_text = StepExporter._build_vod_text(legacy_segs, durations, "raw")

        assert (
            render_vod(_bare_entries(1, 2, "raw"), map_uri=hls.init_name("raw"))
            == legacy_text
        )

    def test_no_playable_segments_yields_empty_entries(self, tmp_storage):
        """"这个 step 还没有可播段"是落盘事实，映射成 404 还是别的由调用方判断（R3）。"""
        assert _bare_entries(1, 2, "raw") == []
