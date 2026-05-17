"""
Lab API（`/lab-f3m8/*`，路径混淆防自动扫描器）

让操作员在一个 step 的 raw 整段视频上选 N 段不重叠的 [start_ms, end_ms]，
后端剪出对应的 mp4 并提交到 Label Studio 创建标注任务。

数据底座：
- 复用 traceback 的 (task_id, step_id) 文件系统约定
- 复用 SegmentFinder 列表/过滤 raw 段
- ffmpeg concat demuxer + libx264 实现 ms 精度裁剪
- urllib.request multipart 上传到 LS（沿用现有 alarm_strategy 的 urllib 风格）

设计要点：
- 整个 submit 同步执行；ffmpeg + LS 上传都在请求线程里跑完
- 单段失败不让整请求失败：HTTP 仍 200，每段在 response 里带 success/error_code
- 仅 raw 轨；processed 轨不送标
- 无新表，无任何持久化状态（除临时 job_dir 下的 mp4 文件）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.services.lab import (
    ClipBuilder,
    ClipBuildError,
    ClipRangeGapError,
    ClipRangeOutOfBoundsError,
    ClipSpec,
    LabelStudioClient,
)
from app.services.lab import runtime_config
from app.services.traceback.segment_finder import SegmentFinder, get_default_base_dir
from app.utils.exceptions import NotFoundError, ValidationError

router = APIRouter(prefix="/lab-f3m8", tags=["lab"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LabClipRange(BaseModel):
    start_ms: int = Field(..., ge=0, description="绝对墙钟 ms（与 traceback timeline 一致）")
    end_ms: int = Field(..., ge=1)
    label: Optional[str] = Field(None, max_length=64, description="透传到 LS task.data 的标注 hint")


class LabSubmitRequest(BaseModel):
    task_id: int
    step_id: int
    project_id: Optional[int] = Field(
        None,
        description="LS project id；不传则使用 settings.label_studio_default_project_id",
    )
    clips: List[LabClipRange] = Field(..., min_length=1)
    keep_artifacts_on_failure: bool = Field(
        True,
        description="任一段失败时是否保留临时 job_dir 下的 mp4，方便手动重试",
    )


class LabClipResultDTO(BaseModel):
    start_ms: int
    end_ms: int
    success: bool
    label_studio_task_id: Optional[int] = None
    duration_ms: Optional[int] = None
    size_bytes: Optional[int] = None
    n_source_segments: Optional[int] = None
    error_code: Optional[str] = None
    error: Optional[str] = None


class LabSubmitResponse(BaseModel):
    task_id: int
    step_id: int
    project_id: int
    job_dir: Optional[str] = None
    total: int
    success_count: int
    failure_count: int
    clips: List[LabClipResultDTO]


class LabHealthResponse(BaseModel):
    configured: bool
    reachable: bool
    error: Optional[str] = None
    label_studio_url: Optional[str] = None
    default_project_id: int


class LabConfigResponse(BaseModel):
    label_studio_url: str
    default_project_id: int
    token_configured: bool          # token 是否已配置（不返回明文）
    source: str                     # "file"（页面改过）| "env"（回退环境变量）


class LabConfigUpdateRequest(BaseModel):
    label_studio_url: str = Field(
        "", description="LS base URL；空表示未配置，非空须以 http:// 或 https:// 开头"
    )
    default_project_id: int = Field(0, ge=0, description="默认 project_id；0 表示无默认值")


# ---------------------------------------------------------------------------
# 校验工具
# ---------------------------------------------------------------------------


def _validate_clips(
    clips: List[LabClipRange],
    *,
    max_clips: int,
    max_clip_ms: int,
    max_total_ms: int,
) -> List[LabClipRange]:
    """按 start_ms 升序排好，校验：单段时长、不重叠、数量、总时长。

    Raises:
        ValidationError: 任一校验失败
    """
    if len(clips) > max_clips:
        raise ValidationError(
            f"Too many clips: {len(clips)} > max {max_clips}",
            field="clips",
        )

    # 按 start_ms 升序（输入未必有序）
    ordered = sorted(clips, key=lambda c: c.start_ms)

    total_ms = 0
    for i, c in enumerate(ordered):
        if c.end_ms <= c.start_ms:
            raise ValidationError(
                f"clip[{i}] end_ms ({c.end_ms}) <= start_ms ({c.start_ms})",
                field="clips",
            )
        duration = c.end_ms - c.start_ms
        if duration > max_clip_ms:
            raise ValidationError(
                f"clip[{i}] duration {duration} ms exceeds max {max_clip_ms} ms",
                field="clips",
            )
        total_ms += duration

        if i > 0 and c.start_ms < ordered[i - 1].end_ms:
            raise ValidationError(
                f"clip[{i}] overlaps with previous "
                f"(start_ms={c.start_ms} < prev.end_ms={ordered[i - 1].end_ms})",
                field="clips",
            )

    if total_ms > max_total_ms:
        raise ValidationError(
            f"Total duration {total_ms} ms exceeds max {max_total_ms} ms",
            field="clips",
        )

    return ordered


def _resolve_project_id(req_project_id: Optional[int], default_pid: int) -> int:
    """req.project_id 优先；为空时 fallback 到 settings 默认；都没就 400。"""
    pid = req_project_id if req_project_id else default_pid
    if not pid or pid <= 0:
        raise ValidationError(
            "project_id is required: pass it in the request body or set "
            "CLEANSIGHT_LABEL_STUDIO_DEFAULT_PROJECT_ID",
            field="project_id",
        )
    return int(pid)


# ---------------------------------------------------------------------------
# 接口 1: 提交导出 + 送标
# ---------------------------------------------------------------------------


@router.post("/submit", response_model=LabSubmitResponse)
async def submit_clips(req: LabSubmitRequest) -> LabSubmitResponse:
    """剪出 N 段 mp4 → 一段一段 POST 到 Label Studio /api/projects/{pid}/import。

    单段失败不让整请求失败；HTTP 仍 200，每段在 clips[] 里携带 success/error_code。
    """
    from app.settings import settings as s

    # ---- LS 配置检查（503）----
    ls_url = runtime_config.get_url()
    ls_token = runtime_config.get_token()
    if not ls_url or not ls_token:
        raise _ls_not_configured()

    # ---- 入参校验（400）----
    project_id = _resolve_project_id(
        req.project_id, runtime_config.get_default_project_id()
    )
    ordered_clips = _validate_clips(
        req.clips,
        max_clips=s.lab_export_max_clips_per_submit,
        max_clip_ms=s.lab_export_max_clip_ms,
        max_total_ms=s.lab_export_max_total_ms,
    )

    # ---- 段存在性（404）----
    finder = SegmentFinder(get_default_base_dir())
    if not finder.list_segments(req.task_id, req.step_id, "raw"):
        raise NotFoundError(
            f"No raw segments for task_id={req.task_id}, step_id={req.step_id}",
            resource_type="Segments",
            resource_id=f"task={req.task_id},step={req.step_id},track=raw",
        )

    # ---- ClipBuilder + LS 客户端 ----
    temp_root = Path(s.lab_export_temp_dir) if s.lab_export_temp_dir else None
    builder = ClipBuilder(
        finder=finder,
        ffmpeg_bin=s.ffmpeg_path,
        temp_root=temp_root,
        preset=s.lab_export_ffmpeg_preset,
        max_duration_ms=s.lab_export_max_clip_ms,
    )
    ls = LabelStudioClient(
        base_url=ls_url,
        token=ls_token,
    )

    # 实际工作放到线程池里：ffmpeg/urlopen 都是阻塞调用
    def _do_work() -> LabSubmitResponse:
        job_dir = builder.new_job_dir()
        results: List[LabClipResultDTO] = []
        any_failure = False

        for c in ordered_clips:
            spec = ClipSpec(
                task_id=req.task_id,
                step_id=req.step_id,
                start_ms=c.start_ms,
                end_ms=c.end_ms,
                label=c.label,
            )
            results.append(_process_one(spec, builder, ls, project_id, job_dir))
            if not results[-1].success:
                any_failure = True

        # 全部成功 / 不要求保留 → 清理
        retained_job_dir: Optional[str] = None
        if any_failure and req.keep_artifacts_on_failure:
            retained_job_dir = str(job_dir)
        else:
            builder.cleanup(job_dir)

        success_count = sum(1 for r in results if r.success)
        return LabSubmitResponse(
            task_id=req.task_id,
            step_id=req.step_id,
            project_id=project_id,
            job_dir=retained_job_dir,
            total=len(results),
            success_count=success_count,
            failure_count=len(results) - success_count,
            clips=results,
        )

    return await run_in_threadpool(_do_work)


def _process_one(
    spec: ClipSpec,
    builder: ClipBuilder,
    ls: LabelStudioClient,
    project_id: int,
    job_dir: Path,
) -> LabClipResultDTO:
    """处理一段：build → 上传。返回单段 DTO（不抛）。"""
    # Step 1: build mp4
    try:
        clip_res = builder.build_one(spec, job_dir)
    except ClipRangeOutOfBoundsError as e:
        return LabClipResultDTO(
            start_ms=spec.start_ms, end_ms=spec.end_ms, success=False,
            error_code="range_out_of_bounds", error=str(e),
        )
    except ClipRangeGapError as e:
        return LabClipResultDTO(
            start_ms=spec.start_ms, end_ms=spec.end_ms, success=False,
            error_code="range_gap", error=str(e),
        )
    except ClipBuildError as e:
        return LabClipResultDTO(
            start_ms=spec.start_ms, end_ms=spec.end_ms, success=False,
            error_code="ffmpeg_failed", error=str(e),
        )

    # Step 2: upload
    meta = {
        "task_id": spec.task_id,
        "step_id": spec.step_id,
        "start_ms": spec.start_ms,
        "end_ms": spec.end_ms,
        "label": spec.label,
        "source": "cleansight",
    }
    ls_res = ls.import_clip(project_id, clip_res.output_path, meta=meta)
    if not ls_res.success:
        return LabClipResultDTO(
            start_ms=spec.start_ms, end_ms=spec.end_ms, success=False,
            duration_ms=clip_res.duration_ms,
            size_bytes=clip_res.size_bytes,
            n_source_segments=clip_res.n_source_segments,
            error_code=ls_res.error_code or "ls_bad_response",
            error=ls_res.error,
        )

    return LabClipResultDTO(
        start_ms=spec.start_ms, end_ms=spec.end_ms, success=True,
        label_studio_task_id=ls_res.task_id,
        duration_ms=clip_res.duration_ms,
        size_bytes=clip_res.size_bytes,
        n_source_segments=clip_res.n_source_segments,
    )


# ---------------------------------------------------------------------------
# 接口 2: 健康探测
# ---------------------------------------------------------------------------


@router.get("/health", response_model=LabHealthResponse)
async def lab_health() -> LabHealthResponse:
    """探测 LS 是否配置 + 是否可达 + token 是否有效。

    不抛异常：未配置时 configured=False，可达性判断时 reachable=False + error。
    """
    ls_url = runtime_config.get_url()
    ls_token = runtime_config.get_token()
    default_pid = runtime_config.get_default_project_id()

    if not ls_url or not ls_token:
        return LabHealthResponse(
            configured=False,
            reachable=False,
            error="Label Studio url / token 未配置（url 可在送标页面设置，token 需后端 env）",
            label_studio_url=ls_url or None,
            default_project_id=default_pid,
        )

    def _ping() -> tuple[bool, Optional[str]]:
        try:
            cli = LabelStudioClient(ls_url, ls_token, timeout=10)
            return cli.ping()
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    reachable, err = await run_in_threadpool(_ping)
    return LabHealthResponse(
        configured=True,
        reachable=reachable,
        error=err,
        label_studio_url=ls_url,
        default_project_id=default_pid,
    )


# ---------------------------------------------------------------------------
# 接口 3: LS 连接配置（url / default_project_id，页面可改，持久化）
# ---------------------------------------------------------------------------


@router.get("/config", response_model=LabConfigResponse)
async def get_lab_config() -> LabConfigResponse:
    """读取当前 LS 连接配置。不做网络探测（探测见 /health）。

    不返回 token 明文，只返回 token_configured 表示 env 里是否已配置。
    """
    return LabConfigResponse(**runtime_config.snapshot())


@router.put("/config", response_model=LabConfigResponse)
async def update_lab_config(req: LabConfigUpdateRequest) -> LabConfigResponse:
    """更新 LS url 与 default_project_id，持久化到文件，重启后保留。

    token 不在此处管理（仅 env）。校验失败抛 400。
    """
    try:
        snap = runtime_config.update(req.label_studio_url, req.default_project_id)
    except ValueError as e:
        raise ValidationError(str(e), field="label_studio_url")
    return LabConfigResponse(**snap)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ls_not_configured() -> Exception:
    """LS 未配置时返回的异常。

    没有专门的 503 异常类；用 fastapi 的 HTTPException 直接抛 503。
    """
    from fastapi import HTTPException

    return HTTPException(
        status_code=503,
        detail={
            "error": "Label Studio not configured",
            "detail": "url 可在送标页面「LS 设置」填写；token 须在后端 env 设置 "
            "CLEANSIGHT_LABEL_STUDIO_TOKEN",
        },
    )
