"""送标服务：校验区间 → 剪片 → 上传 Label Studio → 清理；另有整段导出与 LS 探活。无活体，模块函数。

    ls_url, ls_token = require_label_studio()            # 未配置 → LabelStudioNotConfiguredError
    project_id = resolve_project_id(requested)           # → ValidationError
    clips = validate_clips(ranges)                       # → ValidationError；返回按起点排序后的列表
    out = submit_clips(run, clips, project_id=project_id, ls_url=ls_url, ls_token=ls_token,
                       keep_artifacts_on_failure=True)   # 阻塞：ffmpeg + urlopen，别在事件循环上调
    path = export_step(run, track)                       # 阻塞；产物归调用方删
    reachable, err = ping_label_studio(ls_url, ls_token) # 阻塞；不抛

- **`submit_clips` 不校验**：`clips` 必须是 `validate_clips` 的返回值；「run 有没有 raw 段」由调用方先判。
- 校验文案里的 `clip[{i}]` 是**排序后**下标，不是请求里的原始下标。
- 单段失败不抛，落在 `ClipOutcome.error_code`；只有非预期异常会冒出（此时 job_dir 不清理）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from app.types.exceptions import ValidationError
from app.types.run import RunIdentity

from . import runtime_config
from .clip_builder import (
    ClipBuilder,
    ClipBuildError,
    ClipRangeGapError,
    ClipRangeOutOfBoundsError,
    ClipSpec,
)
from .label_studio_client import LabelStudioClient
from .step_exporter import StepExporter
from .types import (
    ERR_FFMPEG_FAILED,
    ERR_LS_BAD_RESPONSE,
    ERR_RANGE_GAP,
    ERR_RANGE_OUT_OF_BOUNDS,
    ClipOutcome,
    ClipRange,
    SubmitOutcome,
)


class LabelStudioNotConfiguredError(Exception):
    """LS url 或 token 未配置（url 在送标页面设，token 只来自 env）。"""


def require_label_studio() -> Tuple[str, str]:
    """返回生效的 `(url, token)`；任一为空抛 `LabelStudioNotConfiguredError`。"""
    url = runtime_config.get_url()
    token = runtime_config.get_token()
    if not url or not token:
        raise LabelStudioNotConfiguredError("Label Studio not configured")
    return url, token


def resolve_project_id(requested: Optional[int]) -> int:
    """`requested` 优先（0 / None 视为未传），否则取运行时默认值；都没有抛 `ValidationError`。"""
    pid = requested if requested else runtime_config.get_default_project_id()
    if not pid or pid <= 0:
        raise ValidationError(
            "project_id is required: pass it in the request body or set "
            "CLEANSIGHT_LABEL_STUDIO_DEFAULT_PROJECT_ID",
            field="project_id",
        )
    return int(pid)


def validate_clips(clips: Sequence[ClipRange]) -> List[ClipRange]:
    """按 `start_media_ms` 升序排好并校验数量、单段时长、不重叠、总时长；上限取 settings。

    空列表原样放行（HTTP 层 `min_length=1` 已拦）。
    """
    from app.settings import settings

    max_clips = settings.lab_export_max_clips_per_submit
    max_clip_ms = settings.lab_export_max_clip_ms
    max_total_ms = settings.lab_export_max_total_ms

    if len(clips) > max_clips:
        raise ValidationError(
            f"Too many clips: {len(clips)} > max {max_clips}",
            field="clips",
        )

    ordered = sorted(clips, key=lambda c: c.start_media_ms)

    total_ms = 0
    for i, c in enumerate(ordered):
        if c.end_media_ms <= c.start_media_ms:
            raise ValidationError(
                f"clip[{i}] end_media_ms ({c.end_media_ms}) <= start_media_ms ({c.start_media_ms})",
                field="clips",
            )
        duration = c.end_media_ms - c.start_media_ms
        if duration > max_clip_ms:
            raise ValidationError(
                f"clip[{i}] duration {duration} ms exceeds max {max_clip_ms} ms",
                field="clips",
            )
        total_ms += duration

        if i > 0 and c.start_media_ms < ordered[i - 1].end_media_ms:
            raise ValidationError(
                f"clip[{i}] overlaps with previous "
                f"(start_media_ms={c.start_media_ms} "
                f"< prev.end_media_ms={ordered[i - 1].end_media_ms})",
                field="clips",
            )

    if total_ms > max_total_ms:
        raise ValidationError(
            f"Total duration {total_ms} ms exceeds max {max_total_ms} ms",
            field="clips",
        )

    return ordered


def submit_clips(
    run: RunIdentity,
    clips: Sequence[ClipRange],
    *,
    project_id: int,
    ls_url: str,
    ls_token: str,
    keep_artifacts_on_failure: bool,
) -> SubmitOutcome:
    """逐段剪片并上传到 LS project；全成功或不要求保留时删掉 job_dir。"""
    from app.settings import settings as s

    temp_root = Path(s.lab_export_temp_dir) if s.lab_export_temp_dir else None
    builder = ClipBuilder(
        ffmpeg_bin=s.ffmpeg_path,
        temp_root=temp_root,
        preset=s.lab_export_ffmpeg_preset,
        max_duration_ms=s.lab_export_max_clip_ms,
    )
    ls = LabelStudioClient(
        base_url=ls_url,
        token=ls_token,
    )

    job_dir = builder.new_job_dir()
    results: List[ClipOutcome] = []
    for c in clips:
        spec = ClipSpec(
            run=run,
            start_media_ms=c.start_media_ms,
            end_media_ms=c.end_media_ms,
        )
        results.append(_process_one(spec, builder, ls, project_id, job_dir))

    retained: Optional[Path] = None
    if keep_artifacts_on_failure and not all(r.success for r in results):
        retained = job_dir
    else:
        builder.cleanup(job_dir)
    return SubmitOutcome(job_dir=retained, clips=results)


def _process_one(
    spec: ClipSpec,
    builder: ClipBuilder,
    ls: LabelStudioClient,
    project_id: int,
    job_dir: Path,
) -> ClipOutcome:
    """build → 上传，返回单段结果（不抛）。"""
    try:
        clip_res = builder.build_one(spec, job_dir)
    except ClipRangeOutOfBoundsError as e:
        return ClipOutcome(
            start_media_ms=spec.start_media_ms, end_media_ms=spec.end_media_ms, success=False,
            error_code=ERR_RANGE_OUT_OF_BOUNDS, error=str(e),
        )
    except ClipRangeGapError as e:
        return ClipOutcome(
            start_media_ms=spec.start_media_ms, end_media_ms=spec.end_media_ms, success=False,
            error_code=ERR_RANGE_GAP, error=str(e),
        )
    except ClipBuildError as e:
        return ClipOutcome(
            start_media_ms=spec.start_media_ms, end_media_ms=spec.end_media_ms, success=False,
            error_code=ERR_FFMPEG_FAILED, error=str(e),
        )

    # 不带元数据：LS /import 文件上传模式忽略 multipart 里的非文件字段，溯源只剩文件名里的墙钟区间。
    ls_res = ls.import_clip(project_id, clip_res.output_path)
    if not ls_res.success:
        return ClipOutcome(
            start_media_ms=spec.start_media_ms, end_media_ms=spec.end_media_ms, success=False,
            start_ms=clip_res.start_ms, end_ms=clip_res.end_ms,
            duration_ms=clip_res.duration_ms,
            size_bytes=clip_res.size_bytes,
            n_source_segments=clip_res.n_source_segments,
            error_code=ls_res.error_code or ERR_LS_BAD_RESPONSE,
            error=ls_res.error,
        )

    return ClipOutcome(
        start_media_ms=spec.start_media_ms, end_media_ms=spec.end_media_ms, success=True,
        start_ms=clip_res.start_ms, end_ms=clip_res.end_ms,
        label_studio_task_id=ls_res.task_id,
        duration_ms=clip_res.duration_ms,
        size_bytes=clip_res.size_bytes,
        n_source_segments=clip_res.n_source_segments,
    )


def export_step(run: RunIdentity, track: str) -> Path:
    """把该 run 某轨全部落盘段 remux 成单个 mp4，返回产物路径（调用方负责删）。

    异常即 `StepExporter.export` 的：`StepExportNoSegments` / `StepExportInitMissing` /
    `StepExportError`（前两者是它的子类，调用方先接子类）。
    """
    from app.settings import settings as s

    temp_root = Path(s.lab_export_temp_dir) if s.lab_export_temp_dir else None
    exporter = StepExporter(
        ffmpeg_bin=s.ffmpeg_path,
        temp_root=temp_root,
    )
    return exporter.export(run, track)


def ping_label_studio(url: str, token: str) -> Tuple[bool, Optional[str]]:
    """`GET /api/version` 探活（超时 10s）。不抛：任何异常折成 `(False, "类型: 消息")`。"""
    try:
        cli = LabelStudioClient(url, token, timeout=10)
        return cli.ping()
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
