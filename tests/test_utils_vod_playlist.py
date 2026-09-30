"""`app.services.utils.vod_playlist`：VOD 形态 m3u8 的文本渲染。

纯函数，无落盘、无 fixture。两类断言：

1. **全文逐字节**（裸文件名形态）：骨架、EXTINF 格式、ENDLIST 与末尾换行一次钉死；
   段 URI 照渲染不问——"长什么样"是调用方的判断，本模块不生成也不改写。
2. **硬格式约束**：`map_uri` 必填（漏传该是 TypeError，不是运行时才炸）、
   `TARGETDURATION` 用 ceil（round 会违反 RFC 8216）。
"""

import pytest

from app.services.utils.vod_playlist import VodEntry, render_vod


class TestRenderVod:
    def test_bare_filename_form(self):
        """喂 ffmpeg 的形态：相对 URI 解析到同目录的 init 与各段。

        缺 ENDLIST → ffmpeg 当直播流只读 live edge，前面的段全丢；故逐字节比到末尾换行。
        """
        entries = [VodEntry("raw_segment_0.mp4", 1.0), VodEntry("raw_segment_1.mp4", 2.0)]
        assert render_vod(entries, map_uri="raw_init.mp4") == "".join(f"{line}\n" for line in [
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
        ])

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

