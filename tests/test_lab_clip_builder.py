"""ClipBuilder 单元测试：装配与三档拒绝 + ffmpeg 命令形态。

**媒体轴上的定位本身不在这里**（段从哪来、落点、墙钟换算、空洞判据）—— 那是
`MediaTimeline` 的职责，见 `tests/test_media_timeline.py`。本文件只测它之上的两件事：

1. `build_one` 的装配：媒体刻度进、绝对墙钟出；三档拒绝（区间非法 / 出界 / 跨空洞）。
2. `_run_ffmpeg` 的命令与临时清单形态：HLS demuxer 而不是 `-f concat`、清单必须与段同目录
   （裸文件名的相对 URI 才解析得到）、`-ss` 在输出侧、缺 init 时 fail-fast、失败也要清理。

不真正调 ffmpeg —— mock `subprocess.run`。
"""

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from app.services.lab.clip_builder import (
    RAW_TRACK,
    ClipBuildError,
    ClipBuilder,
    ClipRangeGapError,
    ClipRangeOutOfBoundsError,
    ClipSpec,
)
from app.services.utils.media_timeline import MediaTimeline
from app.storage import hls
from factories import seed_hls_segments

TASK_ID = 1
STEP_ID = 1
TS0 = 1_700_000_000_000_000        # 首段墙钟起点（us）
_DEFAULT_EXTINF = 10.0


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------


def _hls_dir() -> Path:
    """`{root}/1/1/hls/` —— 段、init 与临时清单的所在目录。"""
    return hls.init_path(TASK_ID, STEP_ID, "raw").parent


def _seed(items, with_init: bool = True) -> Path:
    return seed_hls_segments(TASK_ID, STEP_ID, items, with_init=with_init)


def _window(start_media_ms: int, end_media_ms: int) -> MediaTimeline:
    return MediaTimeline.load(TASK_ID, STEP_ID, RAW_TRACK).select(
        start_media_ms, end_media_ms
    )


def _contiguous(n: int, extinf_s: float = _DEFAULT_EXTINF) -> List[tuple]:
    """n 个首尾相接的段（墙钟间隔 = EXTINF，即无空洞）。"""
    step_us = int(extinf_s * 1_000_000)
    return [(TS0 + i * step_us, extinf_s) for i in range(n)]


def _spec(start_media_ms: int, end_media_ms: int) -> ClipSpec:
    return ClipSpec(
        task_id=TASK_ID, step_id=STEP_ID,
        start_media_ms=start_media_ms, end_media_ms=end_media_ms,
    )


def _builder(tmp_path: Path) -> ClipBuilder:
    return ClipBuilder(temp_root=tmp_path / ".lab_exports")


def _ok_run(cmd, **kwargs):
    """假 ffmpeg：产出文件也要造出来，否则 `build_one` 会在 stat 那步翻车。"""
    Path(cmd[-1]).write_bytes(b"fake-mp4")
    res = MagicMock()
    res.returncode = 0
    res.stderr = ""
    return res


def _capture(monkeypatch, fake=None):
    """替换 subprocess.run，返回记录 cmd / 临时清单内容的 dict。"""
    captured = {}

    def run(cmd, **kwargs):
        captured["cmd"] = cmd
        m3u8 = next((Path(c) for c in cmd if c.endswith(".m3u8")), None)
        if m3u8 is not None:
            captured["m3u8_path"] = m3u8
            if m3u8.exists():
                captured["m3u8_text"] = m3u8.read_text(encoding="utf-8")
        return (fake or _ok_run)(cmd, **kwargs)

    monkeypatch.setattr("app.services.lab.clip_builder.subprocess.run", run)
    return captured


class TestRouterConstructionStaysInSync:
    """路由构造 `ClipBuilder` 时传的每个 kwarg 都必须还在签名里。

    补的是一个真窟窿：`gap_tolerance_ms` 随判据换输入而退役、`clip_builder` 删了这个参数，
    而 [`routers/lab.py`](../app/routers/lab.py) 漏改仍在传 —— `/lab-f3m8/submit` **一条
    测试都没有**，于是只有线上真请求才会撞上 `TypeError`，整个送标端点 500。

    这条是结构断言（读 AST），不需要起服务、不需要 LS/DB 替身；`/submit` 的行为测试是另一
    回事（目前仍缺，见本仓 update 记录）。
    """

    # 本域所有被路由构造的类。`ClipSpec` 与 `ClipBuilder` 暴露面完全相同——字段刚改过名，
    # 只堵一个等于留另一个。
    TARGETS = {"ClipBuilder": ClipBuilder, "ClipSpec": ClipSpec}

    def test_every_kwarg_exists_in_the_signature(self):
        import ast
        import inspect
        from pathlib import Path as _Path

        source = _Path("app/routers/lab.py").read_text(encoding="utf-8")
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in self.TARGETS
        ]
        found = {node.func.id for node in calls}
        assert found == set(self.TARGETS), (
            f"lab.py 里少了这些构造调用：{sorted(set(self.TARGETS) - found)}；"
            "本用例的前提没了，请复核（是真的不构造了，还是改成别的名字了）"
        )

        for call in calls:
            name = call.func.id
            accepted = set(inspect.signature(self.TARGETS[name]).parameters)
            passed = {kw.arg for kw in call.keywords if kw.arg is not None}
            assert passed <= accepted, (
                f"lab.py:{call.lineno} 给 {name} 传了签名里没有的参数："
                f"{sorted(passed - accepted)}"
            )


# ---------------------------------------------------------------------------
# build_one：装配与三档拒绝
# ---------------------------------------------------------------------------


class TestBuildOne:
    def test_returns_wall_clock_converted_by_the_backend(self, tmp_storage, monkeypatch):
        """入参是媒体刻度，产出带回绝对墙钟 —— 落库与 LS 元数据要的是后者。"""
        _seed(_contiguous(3))
        _capture(monkeypatch)
        job_dir = tmp_storage / "job"
        job_dir.mkdir()

        res = _builder(tmp_storage).build_one(_spec(12_000, 15_000), job_dir)

        assert (res.start_ms, res.end_ms) == (TS0 // 1000 + 12_000, TS0 // 1000 + 15_000)
        assert res.duration_ms == 3_000
        assert res.n_source_segments == 1

    def test_wall_clock_accounts_for_a_gap_before_the_window(self, tmp_storage, monkeypatch):
        """窗口之前有空洞时，同一个媒体刻度对应的墙钟要晚整整一个空洞。

        这是缺陷 #1 的后端一半：前端按「W0 + currentTime」上报会偏早 Σgap，改由后端换算
        之后，偏移由清单逐段算出，空洞不再被吞掉。
        """
        _seed([(TS0, 10.0), (TS0 + 30_000_000, 10.0)])       # 两段之间 20s 空洞
        _capture(monkeypatch)
        job_dir = tmp_storage / "job"
        job_dir.mkdir()

        res = _builder(tmp_storage).build_one(_spec(12_000, 15_000), job_dir)

        # 媒体 12s 落在第二段内 2s 处 → 墙钟 = 第二段起点 + 2s（而不是 W0 + 12s）
        assert res.start_ms == (TS0 + 30_000_000) // 1000 + 2_000

    def test_rejects_a_window_spanning_a_gap(self, tmp_storage, monkeypatch):
        _seed([(TS0, 10.0), (TS0 + 30_000_000, 10.0)])
        _capture(monkeypatch)
        job_dir = tmp_storage / "job"
        job_dir.mkdir()

        with pytest.raises(ClipRangeGapError, match=r"gap of 20\.00s"):
            _builder(tmp_storage).build_one(_spec(5_000, 15_000), job_dir)

    def test_rejects_a_range_outside_the_media_axis(self, tmp_storage, monkeypatch):
        _seed(_contiguous(2))
        _capture(monkeypatch)
        job_dir = tmp_storage / "job"
        job_dir.mkdir()

        with pytest.raises(ClipRangeOutOfBoundsError):
            _builder(tmp_storage).build_one(_spec(60_000, 70_000), job_dir)

    def test_rejects_inverted_range(self, tmp_storage):
        _seed(_contiguous(2))
        with pytest.raises(ClipBuildError, match="Invalid range"):
            _builder(tmp_storage).build_one(_spec(5_000, 5_000), tmp_storage)

    def test_rejects_over_long_clip(self, tmp_storage):
        _seed(_contiguous(2))
        builder = ClipBuilder(temp_root=tmp_storage / ".x", max_duration_ms=1_000)
        with pytest.raises(ClipBuildError, match="exceeds max"):
            builder.build_one(_spec(0, 5_000), tmp_storage)

    def test_duration_is_clamped_when_the_range_runs_past_the_end(
        self, tmp_storage, monkeypatch
    ):
        """区间尾越过轨尾时，`duration_ms` 要跟着收到 `end_ms − start_ms`。

        否则落库与 LS 元数据里的时长比真实 mp4 长——产物只到末段段尾为止，而 `duration_ms`
        还是请求的原值。UI 侧越不过 `<video>.duration`，直调 API 容易踩。
        """
        _seed(_contiguous(2))                       # 媒体轴共 20s
        _capture(monkeypatch)
        job_dir = tmp_storage / "job"
        job_dir.mkdir()

        res = _builder(tmp_storage).build_one(_spec(15_000, 25_000), job_dir)

        assert res.end_ms - res.start_ms == 5_000   # 只剩 5s 可用
        assert res.duration_ms == 5_000             # 不是请求的 10_000


# ---------------------------------------------------------------------------
# _run_ffmpeg：命令与临时清单的形态
# ---------------------------------------------------------------------------


class TestRunFfmpeg:
    def test_writes_m3u8_next_to_the_segments(self, tmp_storage, monkeypatch):
        step_dir = _seed(_contiguous(2))
        window = _window(0, 20_000)
        captured = _capture(monkeypatch)

        _builder(tmp_storage)._run_ffmpeg(_spec(0, 20_000), window, step_dir / "out.mp4")

        text = captured["m3u8_text"]
        assert "#EXTM3U" in text
        assert "#EXT-X-VERSION:7" in text
        assert '#EXT-X-MAP:URI="raw_init.mp4"' in text
        assert "#EXT-X-MEDIA-SEQUENCE:0" in text
        assert "#EXT-X-ENDLIST" in text
        # 段以 basename 出现：HLS demuxer 按清单自身所在目录解析相对 URI
        assert f"raw_segment_{TS0}.mp4" in text
        # EXTINF 用清单真值，不改写（改写对 ffmpeg 是空操作，见模块 docstring）
        assert text.count("#EXTINF:10.000,") == 2

        assert captured["m3u8_path"].parent == step_dir      # 必须与段同目录
        assert not captured["m3u8_path"].exists()            # 跑完即清理

    def test_uses_hls_demuxer_not_concat(self, tmp_storage, monkeypatch):
        """关键回归：决不能回到 `-f concat` 喂 fMP4 段（单独 demux 解不出 codec init）。"""
        step_dir = _seed(_contiguous(1))
        window = _window(0, 10**9)
        captured = _capture(monkeypatch)

        _builder(tmp_storage)._run_ffmpeg(_spec(0, 1_000), window, step_dir / "out.mp4")

        cmd = captured["cmd"]
        assert "concat" not in cmd
        assert cmd[cmd.index("-allowed_extensions") + 1] == "ALL"
        assert cmd[cmd.index("-i") + 1].endswith(".m3u8")

    def test_seek_is_on_the_output_side(self, tmp_storage, monkeypatch):
        """`-ss` 必须在 `-i` 之后。挪到输入侧喂 HLS demuxer + fMP4 会 exit 0 产出零流空壳。"""
        step_dir = _seed(_contiguous(2))
        window = _window(0, 10**9)
        captured = _capture(monkeypatch)

        _builder(tmp_storage)._run_ffmpeg(_spec(0, 1_000), window, step_dir / "out.mp4")

        cmd = captured["cmd"]
        assert cmd.index("-ss") > cmd.index("-i")

    def test_offset_is_relative_to_the_first_selected_segment(self, tmp_storage, monkeypatch):
        """取子集时 ffmpeg 把首段 tfdt 归一到 0 → 偏移 = 目标刻度 − 窗口首段媒体起点。"""
        step_dir = _seed(_contiguous(3))
        spec = _spec(12_345, 15_678)
        window = _window(spec.start_media_ms, spec.end_media_ms)
        captured = _capture(monkeypatch)

        _builder(tmp_storage)._run_ffmpeg(spec, window, step_dir / "out.mp4")

        cmd = captured["cmd"]
        assert cmd[cmd.index("-ss") + 1] == "2.345"          # 12.345 − 10.000
        assert cmd[cmd.index("-to") + 1] == "5.678"          # +3.333 时长

    def test_fail_fast_when_init_missing(self, tmp_storage, monkeypatch):
        step_dir = _seed(_contiguous(1), with_init=False)
        window = _window(0, 10**9)
        called = {"n": 0}

        def counting(cmd, **kwargs):
            called["n"] += 1
            return _ok_run(cmd, **kwargs)

        _capture(monkeypatch, fake=counting)

        with pytest.raises(ClipBuildError, match="raw_init.mp4 missing"):
            _builder(tmp_storage)._run_ffmpeg(_spec(0, 1_000), window, step_dir / "out.mp4")
        assert called["n"] == 0, "init 缺失时不该调用 ffmpeg"

    def test_cleans_up_tmp_m3u8_on_failure(self, tmp_storage, monkeypatch):
        step_dir = _seed(_contiguous(1))
        window = _window(0, 10**9)

        def failing(cmd, **kwargs):
            res = MagicMock()
            res.returncode = 1
            res.stderr = "fake ffmpeg error"
            return res

        captured = _capture(monkeypatch, fake=failing)

        with pytest.raises(ClipBuildError, match="ffmpeg failed"):
            _builder(tmp_storage)._run_ffmpeg(_spec(0, 1_000), window, step_dir / "out.mp4")
        assert not captured["m3u8_path"].exists()
