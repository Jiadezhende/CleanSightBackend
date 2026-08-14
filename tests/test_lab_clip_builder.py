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
    ClipRangeGapError,
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


class _FfmpegCalls:
    """收集 `_run_ffmpeg` 内的多次 `subprocess.run` 调用。

    两步管线（见 `clip_builder._run_ffmpeg`）：

    - `copy_cmd`（第 1 次）：HLS demux → 裸 Annex B H.264，`-c:v copy`，输入是临时 m3u8
    - `encode_cmd`（第 2 次）：裸流重定时 + `-ss/-to` 裁剪 + libx264，输入是 `.h264`

    断言必须**显式指名取哪一步**：早先单槽 `captured["cmd"]` 的写法会被后一次调用覆盖，
    于是「输入是 m3u8」「含 -allowed_extensions」这类只对 copy 步成立的断言静默错位到
    encode 步上。想验两步结构本身就断言 `len(calls)`。
    """

    def __init__(self, on_call=None):
        self.calls: List[List[str]] = []
        self._on_call = on_call

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if self._on_call is not None:
            self._on_call(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    @property
    def copy_cmd(self) -> List[str]:
        assert self.calls, "ffmpeg 未被调用"
        return self.calls[0]

    @property
    def encode_cmd(self) -> List[str]:
        assert len(self.calls) >= 2, f"期望两步 ffmpeg 管线，实际调用 {len(self.calls)} 次"
        return self.calls[1]


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

        def on_call(cmd, **kwargs):
            # m3u8 只喂给 copy 步；encode 步输入是裸 .h264，跳过。
            m3u8 = next((Path(c) for c in cmd if c.endswith(".m3u8")), None)
            if m3u8 is None:
                return
            assert m3u8.exists(), "ffmpeg 调用时临时 m3u8 必须存在"
            captured["m3u8_path"] = m3u8
            captured["m3u8_text"] = m3u8.read_text(encoding="utf-8")

        calls = _FfmpegCalls(on_call)
        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", calls)
        _builder(tmp_path)._run_ffmpeg(spec, segs, out)

        assert "m3u8_text" in captured, "临时 m3u8 从未被喂给 ffmpeg"
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

        calls = _FfmpegCalls()
        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", calls)
        _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")

        cmd = calls.copy_cmd
        # 不能含 concat demuxer
        assert "concat" not in cmd
        # 必须 -allowed_extensions ALL（HLS demuxer 读 .mp4 段需要）
        assert "-allowed_extensions" in cmd
        assert cmd[cmd.index("-allowed_extensions") + 1] == "ALL"
        # 输入文件是 m3u8
        i_idx = cmd.index("-i")
        assert cmd[i_idx + 1].endswith(".m3u8")

    def test_two_pass_structure(self, tmp_path, monkeypatch):
        """锁住两步管线本身：copy 步不重编码，encode 步显式重定时后再裁剪。

        这是绕过段间时基不一致的临时方案（见 clip_builder._run_ffmpeg 的 TEMPORARY 注释）。
        一期时基修复落地、撤销本改造时，本用例应当随之删除——它失败即提示「两步结构没了」，
        那时该确认是有意撤销而非误删。
        """
        ts0 = 1_700_000_000_000_000
        step_dir = _make_step_with_segments(tmp_path, [ts0])
        segs = _make_seg_refs(step_dir, [ts0])
        spec = ClipSpec(task_id=1, step_id=1,
                        start_ms=ts0 // 1000, end_ms=ts0 // 1000 + 1000)

        calls = _FfmpegCalls()
        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", calls)
        _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")

        assert len(calls.calls) == 2, "必须是两步：copy 裸流 + 重定时裁剪"

        copy_cmd = calls.copy_cmd
        # copy 步：不得重编码（否则继承被污染的 PTS，正是本改造要绕开的）
        assert "-c:v" in copy_cmd and copy_cmd[copy_cmd.index("-c:v") + 1] == "copy"
        assert "libx264" not in copy_cmd
        assert copy_cmd[-1].endswith(".h264")

        encode_cmd = calls.encode_cmd
        # -r 必须在 -i 之前（输入选项 = 给无时戳裸流赋帧率）；
        # 放到 -i 之后会变成输出帧率重采样（丢帧/重复帧），语义完全不同。
        assert "-r" in encode_cmd
        assert encode_cmd.index("-r") < encode_cmd.index("-i")
        assert encode_cmd[encode_cmd.index("-i") + 1].endswith(".h264")
        # 重定时基准取自 settings 单一真源，不硬编码
        from app.settings import settings
        assert encode_cmd[encode_cmd.index("-r") + 1] == str(settings.raw_fps)

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
        calls = _FfmpegCalls()
        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", calls)
        _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")

        # -ss/-to 属 encode 步（copy 步不裁剪，整段拷成裸流）
        cmd = calls.encode_cmd
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

        calls = _FfmpegCalls()
        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", calls)
        _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")

        # offset 被 clamp 到 0；end 仍是 (start + duration) - clamped_offset
        # 即 0 → 1.5（duration=1500ms + 原本被截掉的 500ms 偏移）
        cmd = calls.encode_cmd
        assert cmd[cmd.index("-ss") + 1] == "0.000"


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
            # 模拟 ffmpeg 已落下部分裸流才失败——否则「清理了 h264」是平凡成立的假绿。
            h264 = next((Path(c) for c in cmd if c.endswith(".h264")), None)
            if h264 is not None:
                h264.write_bytes(b"partial")
                captured["h264_path"] = h264
            res = MagicMock()
            res.returncode = 1
            res.stderr = "fake ffmpeg error"
            return res

        monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", fake_run)
        with pytest.raises(ClipBuildError, match="ffmpeg failed"):
            _builder(tmp_path)._run_ffmpeg(spec, segs, step_dir / "out.mp4")
        assert not captured["m3u8_path"].exists(), "ffmpeg 失败时也必须清理临时 m3u8"
        assert "h264_path" in captured, "copy 步的裸流路径未被观察到"
        assert not captured["h264_path"].exists(), "ffmpeg 失败时也必须清理中间裸流"


# ---------------------------------------------------------------------------
# 连续性判据：基准取 step 实测节奏（中位数间隔），吸收 fps 漂移、只拒真停顿
# ---------------------------------------------------------------------------


def _ts_from_spacings(spacings_s: List[float]) -> List[int]:
    """由相邻间隔（秒）构造一串 ts_us，起点取一个固定墙钟。"""
    ts0 = 1_700_000_000_000_000
    ts = [ts0]
    for s in spacings_s:
        ts.append(ts[-1] + int(round(s * 1_000_000)))
    return ts


def _continuity_builder(
    tmp_path: Path, step_ts_us: List[int], gap_tolerance_ms: int = 2000
) -> ClipBuilder:
    """构造 builder，其 finder.list_segments 返回整个 step 的段（用于估节奏基准）。"""
    finder = MagicMock()
    finder.base_dir = tmp_path
    finder.list_segments.return_value = _make_seg_refs(tmp_path, step_ts_us)
    return ClipBuilder(
        finder=finder,
        temp_root=tmp_path / ".lab_exports",
        gap_tolerance_ms=gap_tolerance_ms,
    )


class TestValidateContinuity:
    def test_systematic_fps_drift_passes(self, tmp_path):
        """回归：每段墙钟间隔都 >10s（真实 fps<30 的系统漂移）→ 基准≈实测 → 全通过。

        旧逻辑用固定 10.5s 上限会逐段误拒。
        """
        step_ts = _ts_from_spacings([10.6, 10.83, 10.7, 10.9, 10.6])
        builder = _continuity_builder(tmp_path, step_ts)
        segs = _make_seg_refs(tmp_path, step_ts)  # 整窗送裁
        builder._validate_continuity(segs)  # 不抛即通过

    def test_occasional_slowdown_within_tolerance_passes(self, tmp_path):
        """偶发某段变慢（10.83 vs 基准≈10.1）→ excess≈0.7s < 2s → 通过。"""
        step_ts = _ts_from_spacings([10.0, 10.1, 10.83, 10.0])
        builder = _continuity_builder(tmp_path, step_ts)
        builder._validate_continuity(_make_seg_refs(tmp_path, step_ts))

    def test_genuine_stall_still_rejects(self, tmp_path):
        """真停顿：一段间隔 16s、基准≈10s → excess≈6s > 2s → 仍拒，且消息报真实超出量。"""
        step_ts = _ts_from_spacings([10.0, 10.0, 16.0, 10.0])
        builder = _continuity_builder(tmp_path, step_ts)
        with pytest.raises(ClipRangeGapError, match=r"exceeds step rhythm by 6\.0\ds"):
            builder._validate_continuity(_make_seg_refs(tmp_path, step_ts))

    def test_single_selected_segment_no_raise(self, tmp_path):
        """选中窗口仅 1 段 → 无相邻对 → 提前返回不抛。"""
        step_ts = _ts_from_spacings([10.0, 10.0])
        builder = _continuity_builder(tmp_path, step_ts)
        one = _make_seg_refs(tmp_path, step_ts[:1])
        builder._validate_continuity(one)

    def test_two_segment_step_never_rejects(self, tmp_path):
        """整个 step 只有 2 段（仅 1 个间隔样本）→ 基准=该间隔 → excess=0 → 即便间隔很大也不拒。

        无法从单一样本区分漂移与停顿，按「不误拒」处理。
        """
        step_ts = _ts_from_spacings([30.0])  # 唯一间隔 30s
        builder = _continuity_builder(tmp_path, step_ts)
        builder._validate_continuity(_make_seg_refs(tmp_path, step_ts))

    def test_tolerance_is_configurable(self, tmp_path):
        """同一组段：紧容差判停顿、松容差放行。"""
        step_ts = _ts_from_spacings([10.0, 10.0, 11.2, 10.0])  # 11.2 vs 基准 10.0 → excess 1.2s
        segs = _make_seg_refs(tmp_path, step_ts)

        with pytest.raises(ClipRangeGapError):
            _continuity_builder(tmp_path, step_ts, gap_tolerance_ms=500)._validate_continuity(segs)

        _continuity_builder(tmp_path, step_ts, gap_tolerance_ms=2000)._validate_continuity(segs)
