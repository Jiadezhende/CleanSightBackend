"""送标服务 `app.services.lab.service` 单元测试。

剪片与上传用替身（fake ClipBuilder / fake LabelStudioClient，monkeypatch 到 service 模块上），
不跑 ffmpeg、不连 LS。ClipBuilder / LabelStudioClient 自身行为见 test_lab_clip_builder.py。
"""

import inspect
from pathlib import Path
from typing import List

import pytest

from app.services.lab import runtime_config
from app.services.lab import service as lab_service
from app.services.lab.clip_builder import (
    ClipBuilder,
    ClipBuildError,
    ClipRangeGapError,
    ClipRangeOutOfBoundsError,
    ClipResult,
)
from app.services.lab.label_studio_client import LabelStudioClient, LabelStudioTaskResult
from app.services.lab.step_exporter import StepExporter, StepExportNoSegments
from app.services.lab.types import ClipRange
from app.settings import settings
from app.types.exceptions import ValidationError
from factories import make_run


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class FakeBuilder:
    """按区间起点决定结果：`fail_by_start[start] = 异常实例`，其余成功。"""

    instances: List["FakeBuilder"] = []
    fail_by_start: dict = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.job_dir: Path = kwargs["temp_root"] / "job"
        self.cleaned: List[Path] = []
        self.built: list = []
        FakeBuilder.instances.append(self)

    def new_job_dir(self) -> Path:
        self.job_dir.mkdir(parents=True)
        return self.job_dir

    def build_one(self, spec, job_dir):
        self.built.append(spec)
        err = self.fail_by_start.get(spec.start_media_ms)
        if err is not None:
            raise err
        out = job_dir / f"clip_{spec.start_media_ms}.mp4"
        out.write_bytes(b"mp4")
        return ClipResult(
            spec=spec, output_path=out,
            start_ms=1_000_000 + spec.start_media_ms, end_ms=1_000_000 + spec.end_media_ms,
            duration_ms=spec.duration_ms, size_bytes=3, n_source_segments=1,
        )

    def cleanup(self, job_dir):
        self.cleaned.append(job_dir)


class FakeLS:
    """`results_by_name[文件名] = LabelStudioTaskResult`，缺省成功（task_id=42）。"""

    instances: List["FakeLS"] = []
    results_by_name: dict = {}

    def __init__(self, base_url, token, timeout=60):
        self.args = (base_url, token, timeout)
        self.uploads: list = []
        FakeLS.instances.append(self)

    def import_clip(self, project_id, mp4_path):
        self.uploads.append((project_id, mp4_path))
        return self.results_by_name.get(
            mp4_path.name,
            LabelStudioTaskResult(success=True, task_id=42, error=None, error_code=None),
        )


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    FakeBuilder.instances, FakeBuilder.fail_by_start = [], {}
    FakeLS.instances, FakeLS.results_by_name = [], {}
    monkeypatch.setattr(lab_service, "ClipBuilder", FakeBuilder)
    monkeypatch.setattr(lab_service, "LabelStudioClient", FakeLS)
    monkeypatch.setattr(settings, "lab_export_temp_dir", str(tmp_path / "exports"))
    return tmp_path


def _submit(clips, keep=True):
    run = make_run(1, 1)
    return lab_service.submit_clips(
        run, clips, project_id=7, ls_url="http://ls", ls_token="tok",
        keep_artifacts_on_failure=keep,
    )


# ---------------------------------------------------------------------------
# require_label_studio / resolve_project_id
# ---------------------------------------------------------------------------


class TestRequireLabelStudio:
    @pytest.mark.parametrize("url,token", [("", "tok"), ("http://ls", ""), ("", "")])
    def test_missing_url_or_token_raises(self, monkeypatch, url, token):
        monkeypatch.setattr(runtime_config, "get_url", lambda: url)
        monkeypatch.setattr(runtime_config, "get_token", lambda: token)
        with pytest.raises(lab_service.LabelStudioNotConfiguredError):
            lab_service.require_label_studio()

    def test_configured_returns_pair(self, monkeypatch):
        monkeypatch.setattr(runtime_config, "get_url", lambda: "http://ls")
        monkeypatch.setattr(runtime_config, "get_token", lambda: "tok")
        assert lab_service.require_label_studio() == ("http://ls", "tok")


class TestResolveProjectId:
    @pytest.mark.parametrize(
        "requested,default,expected",
        [(5, 9, 5), (None, 9, 9), (0, 9, 9)],   # 0 视同未传
    )
    def test_requested_wins_else_default(self, monkeypatch, requested, default, expected):
        monkeypatch.setattr(runtime_config, "get_default_project_id", lambda: default)
        assert lab_service.resolve_project_id(requested) == expected

    @pytest.mark.parametrize("requested,default", [(None, 0), (0, 0), (-3, 0), (None, -1)])
    def test_none_available_raises(self, monkeypatch, requested, default):
        monkeypatch.setattr(runtime_config, "get_default_project_id", lambda: default)
        with pytest.raises(ValidationError) as ei:
            lab_service.resolve_project_id(requested)
        assert ei.value.field == "project_id"
        assert str(ei.value.message) == (
            "project_id is required: pass it in the request body or set "
            "CLEANSIGHT_LABEL_STUDIO_DEFAULT_PROJECT_ID"
        )


# ---------------------------------------------------------------------------
# validate_clips
# ---------------------------------------------------------------------------


class TestValidateClips:
    @pytest.fixture(autouse=True)
    def _limits(self, monkeypatch):
        monkeypatch.setattr(settings, "lab_export_max_clips_per_submit", 3)
        monkeypatch.setattr(settings, "lab_export_max_clip_ms", 10_000)
        monkeypatch.setattr(settings, "lab_export_max_total_ms", 15_000)

    def _msg(self, clips) -> str:
        with pytest.raises(ValidationError) as ei:
            lab_service.validate_clips(clips)
        assert ei.value.field == "clips"
        return ei.value.message

    def test_empty_passes_through(self):
        assert lab_service.validate_clips([]) == []

    def test_sorted_by_start(self):
        out = lab_service.validate_clips([ClipRange(5_000, 6_000), ClipRange(0, 1_000)])
        assert out == [ClipRange(0, 1_000), ClipRange(5_000, 6_000)]

    def test_too_many(self):
        clips = [ClipRange(i * 10, i * 10 + 5) for i in range(4)]
        assert self._msg(clips) == "Too many clips: 4 > max 3"

    def test_end_not_after_start(self):
        assert self._msg([ClipRange(100, 100)]) == (
            "clip[0] end_media_ms (100) <= start_media_ms (100)"
        )

    def test_single_clip_too_long(self):
        assert self._msg([ClipRange(0, 10_001)]) == (
            "clip[0] duration 10001 ms exceeds max 10000 ms"
        )

    def test_overlap_reports_sorted_index(self):
        # 请求顺序里重叠的那段在下标 0，排序后才是 clip[1]
        msg = self._msg([ClipRange(500, 1_500), ClipRange(0, 1_000)])
        assert msg == "clip[1] overlaps with previous (start_media_ms=500 < prev.end_media_ms=1000)"

    def test_touching_is_not_overlap(self):
        assert len(lab_service.validate_clips([ClipRange(0, 1_000), ClipRange(1_000, 2_000)])) == 2

    def test_bad_range_index_is_sorted_index(self):
        msg = self._msg([ClipRange(9_000, 9_500), ClipRange(3_000, 2_000)])
        assert msg == "clip[0] end_media_ms (2000) <= start_media_ms (3000)"

    def test_total_too_long(self):
        clips = [ClipRange(0, 8_000), ClipRange(10_000, 18_000)]
        assert self._msg(clips) == "Total duration 16000 ms exceeds max 15000 ms"


# ---------------------------------------------------------------------------
# submit_clips
# ---------------------------------------------------------------------------


class TestSubmitClips:
    def test_constructs_from_settings_with_real_signatures(self, fakes, monkeypatch):
        monkeypatch.setattr(settings, "lab_export_ffmpeg_preset", "ultrafast")
        monkeypatch.setattr(settings, "lab_export_max_clip_ms", 123_000)
        _submit([ClipRange(0, 1_000)])

        kw = FakeBuilder.instances[0].kwargs
        assert kw == {
            "ffmpeg_bin": settings.ffmpeg_path,
            "temp_root": Path(settings.lab_export_temp_dir),
            "preset": "ultrafast",
            "max_duration_ms": 123_000,
        }
        # 替身收什么真类也得收：防 kwarg 在真签名里退役而这里漏改
        assert set(kw) <= set(inspect.signature(ClipBuilder).parameters)
        assert FakeLS.instances[0].args == ("http://ls", "tok", 60)
        assert set(inspect.signature(LabelStudioClient).parameters) >= {"base_url", "token"}

    def test_all_success_cleans_job_dir(self, fakes):
        out = _submit([ClipRange(0, 1_000), ClipRange(2_000, 3_000)])
        b = FakeBuilder.instances[0]

        assert out.job_dir is None
        assert b.cleaned == [b.job_dir]
        assert [c.success for c in out.clips] == [True, True]
        first = out.clips[0]
        assert (first.start_media_ms, first.end_media_ms) == (0, 1_000)
        assert (first.start_ms, first.end_ms) == (1_000_000, 1_001_000)
        assert first.label_studio_task_id == 42
        assert (first.duration_ms, first.size_bytes, first.n_source_segments) == (1_000, 3, 1)
        assert first.error_code is None and first.error is None
        assert [p for p, _ in FakeLS.instances[0].uploads] == [7, 7]
        assert [s.run for s in b.built] == [make_run(1, 1)] * 2

    @pytest.mark.parametrize(
        "exc,code",
        [
            (ClipRangeOutOfBoundsError("oob"), "range_out_of_bounds"),
            (ClipRangeGapError("gap"), "range_gap"),
            (ClipBuildError("boom"), "ffmpeg_failed"),
        ],
    )
    def test_build_failure_codes(self, fakes, exc, code):
        FakeBuilder.fail_by_start = {0: exc}
        out = _submit([ClipRange(0, 1_000), ClipRange(2_000, 3_000)])

        failed, ok = out.clips
        assert (failed.success, failed.error_code, failed.error) == (False, code, str(exc))
        assert failed.start_ms is None and failed.duration_ms is None
        assert (failed.start_media_ms, failed.end_media_ms) == (0, 1_000)
        assert ok.success
        # 剪片失败的段不上传
        assert [p.name for _, p in FakeLS.instances[0].uploads] == ["clip_2000.mp4"]

    def test_ls_failure_passes_client_code_and_keeps_wall_clock(self, fakes):
        FakeLS.results_by_name = {
            "clip_0.mp4": LabelStudioTaskResult(
                success=False, task_id=None, error="HTTP 401", error_code="ls_auth"
            ),
        }
        (c,) = _submit([ClipRange(0, 1_000)]).clips
        assert (c.success, c.error_code, c.error) == (False, "ls_auth", "HTTP 401")
        assert (c.start_ms, c.end_ms, c.duration_ms) == (1_000_000, 1_001_000, 1_000)
        assert c.label_studio_task_id is None

    def test_ls_failure_without_code_defaults_to_bad_response(self, fakes):
        FakeLS.results_by_name = {
            "clip_0.mp4": LabelStudioTaskResult(
                success=False, task_id=None, error="weird", error_code=None
            ),
        }
        (c,) = _submit([ClipRange(0, 1_000)]).clips
        assert c.error_code == "ls_bad_response"

    def test_failure_with_keep_retains_job_dir(self, fakes):
        FakeBuilder.fail_by_start = {0: ClipBuildError("x")}
        out = _submit([ClipRange(0, 1_000)], keep=True)
        b = FakeBuilder.instances[0]
        assert out.job_dir == b.job_dir
        assert out.job_dir.is_dir()
        assert b.cleaned == []

    def test_failure_without_keep_cleans_job_dir(self, fakes):
        FakeBuilder.fail_by_start = {0: ClipBuildError("x")}
        out = _submit([ClipRange(0, 1_000)], keep=False)
        b = FakeBuilder.instances[0]
        assert out.job_dir is None
        assert b.cleaned == [b.job_dir]

    def test_clips_processed_in_given_order(self, fakes):
        # submit 不排序也不校验：顺序就是 validate_clips 给的顺序
        _submit([ClipRange(5_000, 6_000), ClipRange(0, 1_000)])
        assert [s.start_media_ms for s in FakeBuilder.instances[0].built] == [5_000, 0]


# ---------------------------------------------------------------------------
# export_step / ping_label_studio
# ---------------------------------------------------------------------------


class TestExportStep:
    def test_constructs_from_settings_and_returns_path(self, monkeypatch, tmp_path):
        captured = {}

        class FakeExporter:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs

            def export(self, run, track):
                captured["call"] = (run, track)
                return tmp_path / "out.mp4"

        monkeypatch.setattr(lab_service, "StepExporter", FakeExporter)
        monkeypatch.setattr(settings, "lab_export_temp_dir", str(tmp_path / "exp"))
        run = make_run(1, 1)

        assert lab_service.export_step(run, "processed") == tmp_path / "out.mp4"
        assert captured["kwargs"] == {
            "ffmpeg_bin": settings.ffmpeg_path, "temp_root": tmp_path / "exp",
        }
        assert set(captured["kwargs"]) <= set(inspect.signature(StepExporter).parameters)
        assert captured["call"] == (run, "processed")

    def test_empty_temp_dir_setting_means_default_root(self, monkeypatch, tmp_path):
        captured = {}

        class FakeExporter:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def export(self, run, track):
                return tmp_path

        monkeypatch.setattr(lab_service, "StepExporter", FakeExporter)
        monkeypatch.setattr(settings, "lab_export_temp_dir", "")
        lab_service.export_step(make_run(1, 1), "raw")
        assert captured["temp_root"] is None

    def test_no_segments_propagates_exporter_error(self, monkeypatch, tmp_path):
        # 真 StepExporter：无段时在调 ffmpeg 之前就抛
        monkeypatch.setattr(settings, "lab_export_temp_dir", str(tmp_path / "exp"))
        with pytest.raises(StepExportNoSegments):
            lab_service.export_step(make_run(1, 1), "raw")


class TestPingLabelStudio:
    def test_delegates_to_client_with_10s_timeout(self, monkeypatch):
        seen = {}

        class FakeClient:
            def __init__(self, base_url, token, timeout=60):
                seen["args"] = (base_url, token, timeout)

            def ping(self):
                return False, "HTTP 401: Unauthorized"

        monkeypatch.setattr(lab_service, "LabelStudioClient", FakeClient)
        assert lab_service.ping_label_studio("http://ls", "tok") == (
            False, "HTTP 401: Unauthorized",
        )
        assert seen["args"] == ("http://ls", "tok", 10)

    def test_exception_folded_into_error(self):
        # 真客户端：空 url 在构造时抛 ValueError
        assert lab_service.ping_label_studio("", "tok") == (
            False, "ValueError: LabelStudioClient: base_url is required",
        )
