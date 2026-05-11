"""
ClipBuilder 单元测试

聚焦 fMP4 改造后 _run_ffmpeg 的关键不变量：
- 输入端用临时 m3u8（HLS demuxer）而不是 -f concat
- 临时 m3u8 必须落在段所在目录，使 EXT-X-MAP 的相对 URI "init.mp4" 能解析
- m3u8 内容含 EXT-X-MAP、所有选中段（basename 引用）、ENDLIST
- 段目录无 init.mp4 时 fail-fast，不调 ffmpeg
- ffmpeg 失败/异常时临时 m3u8 也要被 finally 清理
- ms 精度通过 -ss/-to 传到 ffmpeg；start_ms 早于第一段则 clamp 到 0

不真正调用 ffmpeg —— mock subprocess.run。
"""

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from app.services.lab.clip_builder import (
    ClipBuildError,
    ClipBuilder,
    ClipSpec,
)
from app.services.traceback.segment_finder import SegmentRef


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_step_with_segments(
    tmp_path: Path, segs_ts_us: List[int], with_init: bool = True
) -> Path:
    step_dir = tmp_path / "1" / "1"
    step_dir.mkdir(parents=True)
    for ts in segs_ts_us:
        (step_dir / f"raw_segment_{ts}.mp4").write_bytes(b"fake-fmp4")
    if with_init:
        (step_dir / "init.mp4").write_bytes(b"fake-init")
    return step_dir


def _make_seg_refs(step_dir: Path, segs_ts_us: List[int]) -> List[SegmentRef]:
    return [
        SegmentRef(
            task_id=1,
            step_id=1,
            track="raw",
            filename=f"raw_segment_{ts}.mp4",
            ts_us=ts,
            path=step_dir / f"raw_segment_{ts}.mp4",
        )
        for ts in segs_ts_us
    ]


def _builder(tmp_path: Path) -> ClipBuilder:
    finder = MagicMock()
    finder.base_dir = tmp_path
    return ClipBuilder(finder=finder, temp_root=tmp_path / ".lab_exports")


def _ok_run(cmd, **kwargs):
    res = MagicMock()
    res.returncode = 0
    res.stderr = ""
    return res


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunFfmpegM3u8:
    def test_writes_m3u8_with_init_map_and_segment_list(self, tmp_path, monkeypatch):
        ts0 = 1_700_000_000_000_000
        ts1 = ts0 + 10_000_000
        step_dir = _make_step_with_segments(tmp_path, [ts0, ts1])
        segs = _make_seg_refs(step_dir, [ts0, ts1])
        spec = ClipSpec(
            task_id=1, step_id=1,
            start_ms=ts0 // 1000 + 2345,
            end_ms=ts1 // 1000 + 3789,
        )
        out = step_dir / "out.mp4"

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            m3u8 = next((Path(c) for c in cmd if c.endswith(".m3u8")), None)
            assert m3u8 is not None and m3u8.exists(), \
                "ffmpeg 调用时临时 m3u8 必须存在"
            captured["m3u8_path"] = m3u8
            captured["m3u8_text"] = m3u8.read_text(encoding="utf-8")
            return _ok_run(cmd, **kwargs)

        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", fake_run)
        _builder(tmp_path)._run_ffmpeg(spec, segs, out)

        text = captured["m3u8_text"]
        assert "#EXTM3U" in text
        assert "#EXT-X-VERSION:7" in text
        assert '#EXT-X-MAP:URI="init.mp4"' in text
        # 段以 basename 出现（相对 URI，依赖临时 m3u8 与段同目录）
        assert f"raw_segment_{ts0}.mp4" in text
        assert f"raw_segment_{ts1}.mp4" in text
        assert "#EXT-X-ENDLIST" in text

        # 临时 m3u8 必须与段同目录（init.mp4 的相对 URI 才能解析）
        assert captured["m3u8_path"].parent == step_dir
        # 跑完后必须清理
        assert not captured["m3u8_path"].exists()

    def test_cmd_uses_hls_demuxer_not_concat(self, tmp_path, monkeypatch):
        """关键回归：决不能再回到 -f concat 喂 fMP4 段。"""
        ts0 = 1_700_000_000_000_000
        step_dir = _make_step_with_segments(tmp_path, [ts0])
        segs = _make_seg_refs(step_dir, [ts0])
        spec = ClipSpec(task_id=1, step_id=1,
                        start_ms=ts0 // 1000, end_ms=ts0 // 1000 + 1000)

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _ok_run(cmd, **kwargs)

        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", fake_run)
        _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")

        cmd = captured["cmd"]
        # 不能含 concat demuxer
        assert "concat" not in cmd
        # 必须 -allowed_extensions ALL（HLS demuxer 读 .mp4 段需要）
        assert "-allowed_extensions" in cmd
        assert cmd[cmd.index("-allowed_extensions") + 1] == "ALL"
        # 输入文件是 m3u8
        i_idx = cmd.index("-i")
        assert cmd[i_idx + 1].endswith(".m3u8")

    def test_offset_uses_first_segment_ts(self, tmp_path, monkeypatch):
        ts0 = 1_700_000_000_000_000
        ts1 = ts0 + 10_000_000
        step_dir = _make_step_with_segments(tmp_path, [ts0, ts1])
        segs = _make_seg_refs(step_dir, [ts0, ts1])
        # start_ms = first_seg_ts_ms + 2345 → offset_s = 2.345
        # end_ms   = first_seg_ts_ms + 13789 → end_s   = 13.789
        spec = ClipSpec(
            task_id=1, step_id=1,
            start_ms=ts0 // 1000 + 2345,
            end_ms=ts0 // 1000 + 13789,
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _ok_run(cmd, **kwargs)

        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", fake_run)
        _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")

        cmd = captured["cmd"]
        assert cmd[cmd.index("-ss") + 1] == "2.345"
        assert cmd[cmd.index("-to") + 1] == "13.789"

    def test_clamps_negative_offset_when_start_before_first_seg(
        self, tmp_path, monkeypatch
    ):
        ts0 = 1_700_000_000_000_000
        step_dir = _make_step_with_segments(tmp_path, [ts0])
        segs = _make_seg_refs(step_dir, [ts0])
        spec = ClipSpec(
            task_id=1, step_id=1,
            start_ms=ts0 // 1000 - 500,
            end_ms=ts0 // 1000 + 1000,
        )

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _ok_run(cmd, **kwargs)

        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", fake_run)
        _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")

        # offset 被 clamp 到 0；end 仍是 (start + duration) - clamped_offset
        # 即 0 → 1.5（duration=1500ms + 原本被截掉的 500ms 偏移）
        assert captured["cmd"][captured["cmd"].index("-ss") + 1] == "0.000"


class TestRunFfmpegFailFast:
    def test_fail_fast_when_init_mp4_missing(self, tmp_path, monkeypatch):
        ts0 = 1_700_000_000_000_000
        step_dir = _make_step_with_segments(tmp_path, [ts0], with_init=False)
        segs = _make_seg_refs(step_dir, [ts0])
        spec = ClipSpec(task_id=1, step_id=1,
                        start_ms=ts0 // 1000,
                        end_ms=ts0 // 1000 + 1000)

        called = {"n": 0}

        def fake_run(cmd, **kwargs):
            called["n"] += 1
            return _ok_run(cmd, **kwargs)

        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", fake_run)
        with pytest.raises(ClipBuildError, match="init.mp4 missing"):
            _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")
        assert called["n"] == 0, "init 缺失时不应调用 ffmpeg"

    def test_cleans_up_tmp_m3u8_on_ffmpeg_failure(self, tmp_path, monkeypatch):
        ts0 = 1_700_000_000_000_000
        step_dir = _make_step_with_segments(tmp_path, [ts0])
        segs = _make_seg_refs(step_dir, [ts0])
        spec = ClipSpec(task_id=1, step_id=1,
                        start_ms=ts0 // 1000,
                        end_ms=ts0 // 1000 + 1000)

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["m3u8_path"] = next(Path(c) for c in cmd if c.endswith(".m3u8"))
            res = MagicMock()
            res.returncode = 1
            res.stderr = "fake ffmpeg error"
            return res

        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", fake_run)
        with pytest.raises(ClipBuildError, match="ffmpeg failed"):
            _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")
        assert not captured["m3u8_path"].exists(), "ffmpeg 失败时也必须清理临时 m3u8"
