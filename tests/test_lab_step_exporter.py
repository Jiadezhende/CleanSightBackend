"""
StepExporter 单元测试

聚焦整段导出的关键不变量（每一条对应一个会静默产出坏视频的坑）：
- EXTINF 取自写入侧 playlist 真值，不是文件名 ts 差重推
- 磁盘上有但 playlist 里没有的段（在途段）被过滤
- 临时 m3u8 落在 step 目录，使 EXT-X-MAP 的相对 URI "init.mp4" 能解析
- 必须写 #EXT-X-ENDLIST（缺了 ffmpeg 当直播只读 live edge，前面的段全丢）
- 走 HLS demuxer 而非 -f concat（fMP4 fragment 无 moov）
- `-c copy`：段本就是 H.264，整段导出决不能重编码
- init.mp4 缺失时 fail-fast，不调 ffmpeg
- ffmpeg 失败/异常时临时 m3u8 也要被 finally 清理
- track 参数真的选到对应轨

不真正调用 ffmpeg —— mock subprocess.run（I/O 边界不硬测，见 DEVELOPMENT.md）。
"""

import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from app.services.lab.step_exporter import (
    StepExporter,
    StepExportError,
    StepExportInitMissing,
    StepExportNoSegments,
)
from app.services.traceback.segment_finder import SegmentFinder, SegmentRef


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

TS0 = 1_700_000_000_000_000
TS1 = TS0 + 10_000_000
TS2 = TS1 + 10_000_000


def _make_step(
    tmp_path: Path,
    segs_ts_us: List[int],
    *,
    track: str = "raw",
    with_init: bool = True,
    playlist_ts: List[int] = None,
    durations: List[float] = None,
) -> Path:
    """在 {tmp}/1/1/ 造 step 目录。

    playlist_ts 为 None 时 playlist 收录全部段；显式传入可制造"在途段"。
    """
    step_dir = tmp_path / "1" / "1"
    step_dir.mkdir(parents=True, exist_ok=True)
    for ts in segs_ts_us:
        (step_dir / f"{track}_segment_{ts}.mp4").write_bytes(b"fake-fmp4")
    if with_init:
        (step_dir / f"{track}_init.mp4").write_bytes(b"fake-init")

    in_playlist = segs_ts_us if playlist_ts is None else playlist_ts
    durs = durations or [10.0] * len(in_playlist)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-TARGETDURATION:10",
        f"#EXT-X-MAP:URI={track}_init.mp4",
    ]
    for ts, d in zip(in_playlist, durs):
        lines.append(f"#EXTINF:{d:.3f},")
        lines.append(f"{track}_segment_{ts}.mp4")
    (step_dir / f"{track}_playlist.m3u8").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return step_dir


def _exporter(tmp_path: Path) -> StepExporter:
    """用真 SegmentFinder（要读磁盘）+ 独立 temp_root。"""
    return StepExporter(
        finder=SegmentFinder(tmp_path),
        ffmpeg_bin="/fake/ffmpeg",
        temp_root=tmp_path / ".lab_exports",
    )


def _capture_run(monkeypatch, returncode: int = 0, touch_output: bool = True):
    """替换 subprocess.run，捕获 cmd + 调用时的 m3u8 文本。"""
    captured = {"calls": 0}

    def fake_run(cmd, **kwargs):
        captured["calls"] += 1
        captured["cmd"] = cmd
        m3u8 = next((Path(c) for c in cmd if c.endswith(".m3u8")), None)
        assert m3u8 is not None and m3u8.exists(), "ffmpeg 调用时临时 m3u8 必须存在"
        captured["m3u8_path"] = m3u8
        captured["m3u8_text"] = m3u8.read_text(encoding="utf-8")
        out = Path(cmd[-1])
        if touch_output and returncode == 0:
            out.write_bytes(b"fake-mp4")
        res = MagicMock()
        res.returncode = returncode
        res.stderr = "" if returncode == 0 else "boom"
        return res

    monkeypatch.setattr(
        "app.services.lab.step_exporter.subprocess.run", fake_run
    )
    return captured


# ---------------------------------------------------------------------------
# m3u8 内容
# ---------------------------------------------------------------------------


class TestVodPlaylist:
    def test_writes_map_endlist_and_all_segments(self, tmp_path, monkeypatch):
        step_dir = _make_step(tmp_path, [TS0, TS1])
        cap = _capture_run(monkeypatch)

        _exporter(tmp_path).export(1, 1, "raw")

        text = cap["m3u8_text"]
        assert "#EXTM3U" in text
        assert "#EXT-X-VERSION:7" in text
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
        assert '#EXT-X-MAP:URI="raw_init.mp4"' in text
        # 段以 basename 出现（相对 URI，依赖临时 m3u8 与段同目录）
        assert f"raw_segment_{TS0}.mp4" in text
        assert f"raw_segment_{TS1}.mp4" in text
        # 缺 ENDLIST → ffmpeg 当直播流只读 live edge，前面的段全丢
        assert "#EXT-X-ENDLIST" in text

        # 临时 m3u8 必须与段同目录（init.mp4 的相对 URI 才能解析）
        assert cap["m3u8_path"].parent == step_dir
        # 跑完后必须清理
        assert not cap["m3u8_path"].exists()

    def test_extinf_comes_from_playlist_not_ts_delta(self, tmp_path, monkeypatch):
        """EXTINF 是时长唯一真值；用 ts 差重推会与 fragment 媒体时长对不上。"""
        # ts 间隔 10s，但 playlist 里的真实 EXTINF 是 9.8 / 7.5
        _make_step(tmp_path, [TS0, TS1], durations=[9.800, 7.500])
        cap = _capture_run(monkeypatch)

        _exporter(tmp_path).export(1, 1, "raw")

        text = cap["m3u8_text"]
        assert "#EXTINF:9.800," in text
        assert "#EXTINF:7.500," in text
        assert "#EXTINF:10.000," not in text
        # TARGETDURATION = ceil(max EXTINF)
        assert "#EXT-X-TARGETDURATION:10" in text

    def test_in_flight_segments_filtered(self, tmp_path, monkeypatch):
        """磁盘上有、playlist 里没有 = 在途段（transcode+append 未完成），必须过滤。"""
        _make_step(tmp_path, [TS0, TS1, TS2], playlist_ts=[TS0, TS1])
        cap = _capture_run(monkeypatch)

        _exporter(tmp_path).export(1, 1, "raw")

        text = cap["m3u8_text"]
        assert f"raw_segment_{TS0}.mp4" in text
        assert f"raw_segment_{TS1}.mp4" in text
        assert f"raw_segment_{TS2}.mp4" not in text

    def test_processed_track_selects_processed_segments(self, tmp_path, monkeypatch):
        _make_step(tmp_path, [TS0, TS1], track="raw")
        _make_step(tmp_path, [TS0, TS1], track="processed")
        cap = _capture_run(monkeypatch)

        _exporter(tmp_path).export(1, 1, "processed")

        text = cap["m3u8_text"]
        assert f"processed_segment_{TS0}.mp4" in text
        assert "raw_segment_" not in text


# ---------------------------------------------------------------------------
# ffmpeg 命令
# ---------------------------------------------------------------------------


class TestFfmpegCmd:
    def test_remux_only_never_reencodes(self, tmp_path, monkeypatch):
        """段落盘时已是 H.264/yuv420p，整段导出只换容器；重编码即画质白掉一次。"""
        _make_step(tmp_path, [TS0])
        cap = _capture_run(monkeypatch)

        _exporter(tmp_path).export(1, 1, "raw")

        cmd = cap["cmd"]
        assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
        assert "libx264" not in cmd
        assert "-crf" not in cmd
        # moov 前置，边下边播 / 拖动 seek
        assert "+faststart" in cmd

    def test_uses_hls_demuxer_not_concat(self, tmp_path, monkeypatch):
        """关键回归：fMP4 fragment 无 moov，-f concat 必失败。"""
        _make_step(tmp_path, [TS0])
        cap = _capture_run(monkeypatch)

        _exporter(tmp_path).export(1, 1, "raw")

        cmd = cap["cmd"]
        assert "concat" not in cmd
        # HLS demuxer 读 .mp4 段需要
        assert "-allowed_extensions" in cmd
        assert cmd[cmd.index("-allowed_extensions") + 1] == "ALL"
        assert cmd[0] == "/fake/ffmpeg"

    def test_output_lands_in_temp_root(self, tmp_path, monkeypatch):
        _make_step(tmp_path, [TS0])
        cap = _capture_run(monkeypatch)

        out = _exporter(tmp_path).export(1, 1, "raw")

        assert out.parent == tmp_path / ".lab_exports"
        assert out.exists()
        assert Path(cap["cmd"][-1]) == out


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


class TestFailures:
    def test_missing_init_fails_fast_without_ffmpeg(self, tmp_path, monkeypatch):
        _make_step(tmp_path, [TS0], with_init=False)
        cap = _capture_run(monkeypatch)

        with pytest.raises(StepExportInitMissing):
            _exporter(tmp_path).export(1, 1, "raw")

        assert cap["calls"] == 0

    def test_no_segments_on_disk(self, tmp_path, monkeypatch):
        (tmp_path / "1" / "1").mkdir(parents=True)
        cap = _capture_run(monkeypatch)

        with pytest.raises(StepExportNoSegments):
            _exporter(tmp_path).export(1, 1, "raw")

        assert cap["calls"] == 0

    def test_all_segments_in_flight(self, tmp_path, monkeypatch):
        """段都在磁盘上但一个都没进 playlist —— 无可播内容，不能产出空 mp4。"""
        _make_step(tmp_path, [TS0, TS1], playlist_ts=[])
        cap = _capture_run(monkeypatch)

        with pytest.raises(StepExportNoSegments):
            _exporter(tmp_path).export(1, 1, "raw")

        assert cap["calls"] == 0

    def test_ffmpeg_failure_cleans_tmp_m3u8_and_output(self, tmp_path, monkeypatch):
        step_dir = _make_step(tmp_path, [TS0])
        cap = _capture_run(monkeypatch, returncode=1)

        with pytest.raises(StepExportError):
            _exporter(tmp_path).export(1, 1, "raw")

        assert not cap["m3u8_path"].exists()
        assert not list(step_dir.glob(".export_*.m3u8"))
        assert not list((tmp_path / ".lab_exports").glob("*.mp4"))

    def test_ffmpeg_binary_missing_cleans_tmp_m3u8(self, tmp_path, monkeypatch):
        step_dir = _make_step(tmp_path, [TS0])

        def boom(cmd, **kwargs):
            raise FileNotFoundError("no ffmpeg")

        monkeypatch.setattr(
            "app.services.lab.step_exporter.subprocess.run", boom
        )

        with pytest.raises(StepExportError):
            _exporter(tmp_path).export(1, 1, "raw")

        assert not list(step_dir.glob(".export_*.m3u8"))


# ---------------------------------------------------------------------------
# 孤儿回收
# ---------------------------------------------------------------------------


class TestOrphanSweep:
    def test_sweeps_stale_exports_keeps_fresh(self, tmp_path, monkeypatch):
        """客户端中途断开时 BackgroundTask 不保证跑到，需要这层兜底。"""
        _make_step(tmp_path, [TS0])
        exports = tmp_path / ".lab_exports"
        exports.mkdir(parents=True, exist_ok=True)

        stale = exports / "step_9_9_raw_deadbeef.mp4"
        fresh = exports / "step_8_8_raw_cafebabe.mp4"
        stale.write_bytes(b"x")
        fresh.write_bytes(b"x")
        old = time.time() - 31 * 60
        import os

        os.utime(stale, (old, old))

        _capture_run(monkeypatch)
        _exporter(tmp_path).export(1, 1, "raw")

        assert not stale.exists()
        assert fresh.exists()
