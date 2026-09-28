"""
Lab API（`/lab-f3m8/*`，路径混淆防自动扫描器）：送标任务清单、逐帧类别概率、送标、整段下载、LS 探活 / 配置。

- 送标（POST /submit）：在一个 step 的 raw 轨上选 N 段不重叠的媒体区间，剪成 mp4 提交到 Label Studio；
  流程在 `services/lab/service.py`，router 只做检查顺序（503 → 400 → 400 → 404）与 DTO 映射。
  单段失败不让整请求失败：HTTP 仍 200，每段带 success / error_code。
- 整段下载（GET /download）：某轨全部落盘段 `-c copy` remux 成单个 mp4（`service.export_step`）。
- (task_id, step_id, 可选 run_id) → 入口处 `resolve_run` 解析一次 run，之后只用这个 run。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.db import tasks as db_tasks
from app.types.run import RunIdentity
from app.services.lab import runtime_config as lab_config
from app.services.lab import service as lab_service
from app.services.lab.step_exporter import (
    StepExportError,
    StepExportInitMissing,
    StepExportNoSegments,
)
from app.services.lab.types import ClipRange
from app.storage import hls
from app.storage import inference as inference_store
from app.storage import runs
from app.storage import tasks as step_tasks
from app.types.exceptions import NotFoundError, ValidationError

from .utils.runs import resolve_run, resolve_timeline

router = APIRouter(prefix="/lab-f3m8", tags=["lab"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LabClipRange(BaseModel):
    """送标区间，用**媒体坐标**表达（`<video>.currentTime × 1000`）。

    不收墙钟：媒体轴是压紧的墙钟（断流停顿在它上面不存在），浏览器手上只有媒体轴上的量，
    `W0 + currentTime` 这个换算只在从没断过流时成立。墙钟由后端用清单换算，随响应带回。
    """

    start_media_ms: int = Field(
        ..., ge=0, description="相对该 step raw 轨媒体轴起点的 ms（= video.currentTime×1000）"
    )
    end_media_ms: int = Field(..., ge=1)


class LabSubmitRequest(BaseModel):
    task_id: int
    step_id: int
    run_id: Optional[int] = Field(None, description="锁定哪个 run；缺省 = 该 step 最新可见 run")
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
    # 请求原样回显（失败时也有），供前端对号入座
    start_media_ms: int
    end_media_ms: int
    # 后端由清单换算出的绝对墙钟；只有走到"选中了段"那一步才算得出，故可空
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
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
    run_id: int
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
    task_source: str                # "db"（按数据库）| "storage"（按实际存储）
    token_configured: bool          # token 是否已配置（不返回明文）
    source: str                     # "file"（页面改过）| "env"（回退环境变量）


class LabConfigUpdateRequest(BaseModel):
    label_studio_url: str = Field(
        "", description="LS base URL；空表示未配置，非空须以 http:// 或 https:// 开头"
    )
    default_project_id: int = Field(0, ge=0, description="默认 project_id；0 表示无默认值")
    task_source: Optional[str] = Field(
        None,
        description='任务列表数据来源 "db" | "storage"；不传表示不修改（DB 挂了可切 storage）',
    )


class LabTaskItem(BaseModel):
    task_id: int
    source_ip: Optional[str] = None
    current_step: Optional[str] = None
    step_id: Optional[int] = None
    status: Optional[str] = None
    updated_time: Optional[int] = None
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    raw_steps: List[int] = Field(default_factory=list)
    # raw_steps 各自最新可见 run 的 run_id，键为 step_id；后续请求带上它即锁定同一个 run
    run_ids: Dict[int, int] = Field(default_factory=dict)
    has_raw_segments: bool = False
    has_current_step_raw: bool = False
    offline_steps: List[int] = Field(default_factory=list)  # raw_steps 中有离线推理结果的 step


class LabTaskListResponse(BaseModel):
    total: int
    tasks: List[LabTaskItem]


# ---------------------------------------------------------------------------
# 任务清单组装
# ---------------------------------------------------------------------------


def _optional_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _list_raw_runs(task_id: int) -> List[RunIdentity]:
    """该 task 各 step 最新可见 run 中有 raw 段的那些（按 step 升序）。送标只吃 raw。

    「raw 轨非空才收」同时滤掉建了目录没写成段的 step（点开是黑屏）。
    """
    return [
        run for run in runs.query_latest_by_step(task_id)
        if hls.query_has_segments(run, "raw")
    ]


def _list_offline_steps(raw_runs: List[RunIdentity]) -> List[int]:
    """`raw_runs` 中有离线推理结果的 step。"""
    return [run.step_id for run in raw_runs if inference_store.query_has_offline_results(run)]


def _task_row_to_item(row: db_tasks.DBTask) -> LabTaskItem:
    task_id = int(row.task_id)
    step_id = _optional_int(row.current_step)
    raw_runs = _list_raw_runs(task_id)
    raw_steps = [run.step_id for run in raw_runs]
    has_current_step_raw = step_id is not None and step_id in raw_steps

    return LabTaskItem(
        task_id=task_id,
        source_ip=row.source_ip,
        current_step=str(row.current_step) if row.current_step is not None else None,
        step_id=step_id,
        status=row.status,
        updated_time=_optional_int(row.updated_time),
        start_time=_optional_int(row.start_time),
        end_time=_optional_int(row.end_time),
        raw_steps=raw_steps,
        run_ids={run.step_id: run.run_id for run in raw_runs},
        has_raw_segments=bool(raw_steps),
        has_current_step_raw=has_current_step_raw,
        offline_steps=_list_offline_steps(raw_runs),
    )


def _storage_task_to_item(task_id: int, raw_runs: List[RunIdentity]) -> LabTaskItem:
    """从文件系统信息构造 LabTaskItem（存储模式）。

    DB 才有的字段（source_ip/status/current_step）无从得知：
    - source_ip=None, status="unknown", step_id/current_step 留空（不推断）
    - start_time = 各 raw 轨首段起点的最小值；updated_time = 各 raw 轨**段尾**的最大值（ms）
    """
    spans = [sp for sp in (hls.query_span(run, ("raw",)) for run in raw_runs) if sp is not None]

    return LabTaskItem(
        task_id=task_id,
        source_ip=None,
        current_step=None,
        step_id=None,
        status="unknown",
        updated_time=max(sp.end_ms for sp in spans) if spans else None,
        start_time=min(sp.start_ms for sp in spans) if spans else None,
        end_time=None,
        raw_steps=[run.step_id for run in raw_runs],
        run_ids={run.step_id: run.run_id for run in raw_runs},
        has_raw_segments=True,
        has_current_step_raw=False,
        offline_steps=_list_offline_steps(raw_runs),
    )


def _list_storage_tasks(
    q: Optional[str], limit: int, offset: int
) -> tuple[int, List[LabTaskItem]]:
    """直接枚举存储目录列任务，完全不碰 DB（DB 挂了也能工作）。

    只收磁盘上有 raw 段的 task（无 raw 段对送标无意义）。
    q 非空时按 str(task_id) 子串过滤（存储模式下 ip/status 不可知）。
    排序 updated_time desc, task_id desc，再 offset/limit 切片。
    """
    needle = (q or "").strip()

    items: List[LabTaskItem] = []
    for task_id in step_tasks.list_task_ids():  # 已跳过 .lab_exports 等非数字目录
        if needle and needle not in str(task_id):
            continue
        raw_runs = _list_raw_runs(task_id)
        if not raw_runs:
            continue
        items.append(_storage_task_to_item(task_id, raw_runs))

    items.sort(key=lambda it: (it.updated_time or 0, it.task_id), reverse=True)
    total = len(items)
    return total, items[offset : offset + limit]


# ---------------------------------------------------------------------------
# 接口 0: 任务列表
# ---------------------------------------------------------------------------


@router.get("/tasks", response_model=LabTaskListResponse)
async def list_lab_tasks(
    q: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> LabTaskListResponse:
    """列出送标页面的任务。

    数据来源由运行时开关 task_source 决定（GET/PUT /lab-f3m8/config）：
    - "db"（默认）：查 clean_task 表 + 文件系统补 raw 段信息（原行为）
    - "storage"：直接枚举存储目录，不碰 DB（业务库挂了时的兜底）
    """
    if lab_config.get_task_source() == "storage":
        total, tasks = _list_storage_tasks(q, limit, offset)
        return LabTaskListResponse(total=total, tasks=tasks)

    total, rows = db_tasks.query_task_page(q, limit=limit, offset=offset)
    return LabTaskListResponse(
        total=total,
        tasks=[_task_row_to_item(row) for row in rows],
    )


# ---------------------------------------------------------------------------
# 接口 0.5: 离线分割模型的逐帧类别概率（label_probs.npz）
# ---------------------------------------------------------------------------


class LabelProbsRequest(BaseModel):
    task_id: int
    step_id: int
    run_id: Optional[int] = None  # 锁定哪个 run；缺省 = 该 step 最新可见 run
    track: Literal["raw", "processed"] = "raw"


class LabelProbsResponse(BaseModel):
    task_id: int
    step_id: int
    run_id: int
    track: str
    media_duration_ms: int
    labels: List[str]
    media_ms: List[int]  # [T]
    probs: List[List[float]]  # [C][T]：按类分列，一类一条曲线


def _label_probs_view(req: LabelProbsRequest) -> LabelProbsResponse:
    """读该 run 的逐帧类别概率，帧 ts 换算到 `track` 轨的媒体刻度；没有产物返回空数组。"""
    run, timeline = resolve_timeline(req.task_id, req.step_id, req.run_id, req.track)
    lp = inference_store.read_label_probs(run)
    if lp is None:
        labels: List[str] = []
        media_ms: List[int] = []
        probs: List[List[float]] = []
    else:
        labels = list(lp.labels)
        media_ms = [timeline.media_ms_at(int(round(float(t) * 1000))) for t in lp.ts]
        probs = lp.probs.T.round(3).tolist()
    return LabelProbsResponse(
        task_id=req.task_id, step_id=req.step_id, run_id=run.run_id, track=req.track,
        media_duration_ms=timeline.duration_ms,
        labels=labels, media_ms=media_ms, probs=probs,
    )


@router.post("/label-probs", response_model=LabelProbsResponse)
def get_label_probs(req: LabelProbsRequest) -> LabelProbsResponse:
    """离线分割模型逐帧 softmax（可视化旁路），时间已换算为媒体刻度。"""
    return _label_probs_view(req)


# ---------------------------------------------------------------------------
# 接口 1: 提交导出 + 送标
# ---------------------------------------------------------------------------


@router.post("/submit", response_model=LabSubmitResponse)
async def submit_clips(req: LabSubmitRequest) -> LabSubmitResponse:
    """剪出 N 段 mp4 → 一段一段 POST 到 Label Studio /api/projects/{pid}/import。

    单段失败不让整请求失败；HTTP 仍 200，每段在 clips[] 里携带 success/error_code。
    """
    # ---- LS 配置检查（503）----
    try:
        ls_url, ls_token = lab_service.require_label_studio()
    except lab_service.LabelStudioNotConfiguredError:
        raise _ls_not_configured() from None

    # ---- 入参校验（400）----
    project_id = lab_service.resolve_project_id(req.project_id)
    ordered_clips = lab_service.validate_clips(
        [ClipRange(c.start_media_ms, c.end_media_ms) for c in req.clips]
    )

    # ---- 段存在性（404）----
    run = resolve_run(req.task_id, req.step_id, req.run_id)
    if run is None or not hls.query_has_segments(run, "raw"):
        raise NotFoundError(
            f"No raw segments for task_id={req.task_id}, step_id={req.step_id}",
            resource_type="Segments",
            resource_id=f"task={req.task_id},step={req.step_id},track=raw",
        )

    # ffmpeg / urlopen 都是阻塞调用
    outcome = await run_in_threadpool(
        lab_service.submit_clips,
        run,
        ordered_clips,
        project_id=project_id,
        ls_url=ls_url,
        ls_token=ls_token,
        keep_artifacts_on_failure=req.keep_artifacts_on_failure,
    )
    results = [LabClipResultDTO(**dataclasses.asdict(o)) for o in outcome.clips]
    success_count = sum(1 for r in results if r.success)
    return LabSubmitResponse(
        task_id=req.task_id,
        step_id=req.step_id,
        run_id=run.run_id,
        project_id=project_id,
        job_dir=str(outcome.job_dir) if outcome.job_dir is not None else None,
        total=len(results),
        success_count=success_count,
        failure_count=len(results) - success_count,
        clips=results,
    )


# ---------------------------------------------------------------------------
# 接口 2: 整段下载
# ---------------------------------------------------------------------------


@router.get("/download")
async def download_step_video(
    task_id: int = Query(..., description="任务 id"),
    step_id: int = Query(..., description="洗消步骤 id"),
    track: str = Query(
        default="processed",
        pattern="^(raw|processed)$",
        description="processed=带检测框，raw=原始画面",
    ),
    run_id: Optional[int] = Query(None, description="锁定哪个 run；缺省 = 该 step 最新可见 run"),
):
    """下载某 step 某轨的整段录像（单个 mp4，attachment）。

    段落盘时已是 H.264/yuv420p，这里纯 `-c copy` 换容器——磁盘速度、零 CPU、
    零二次画质损失，产物是通用 mp4（faststart，可拖动 seek）。

    要 ms 精度区间请走 `/lab-f3m8/submit` 那条（ClipBuilder + libx264），
    与本接口的零成本 remux 性质不同，不合并。

    step 仍在录制时下载 = 拿到当前已落盘的部分（在途段被过滤，不会产出坏文件）。
    """
    run = resolve_run(task_id, step_id, run_id)
    if run is None:
        raise NotFoundError(
            f"No {track} segments for task_id={task_id}, step_id={step_id}",
            resource_type="Segments",
            resource_id=f"task={task_id},step={step_id},track={track}",
        )
    try:
        # ffmpeg 是阻塞调用，扔线程池（与 /submit 同样式）
        output_path = await run_in_threadpool(lab_service.export_step, run, track)
    except StepExportNoSegments as e:
        raise NotFoundError(
            str(e),
            resource_type="Segments",
            resource_id=f"task={task_id},step={step_id},track={track}",
        ) from e
    except StepExportInitMissing as e:
        # 与 /traceback/task/{id}/playlist.m3u8 同码同措辞：该 step 不可用，服务端无法自愈
        raise HTTPException(
            status_code=503,
            detail={"error": "HLS init segment missing", "detail": str(e)},
        ) from e
    except StepExportError as e:
        logger.error(
            "[Lab] step export failed: task=%s step=%s track=%s: %s",
            task_id, step_id, track, e,
        )
        raise HTTPException(status_code=500, detail=f"Export failed: {e}") from e

    # filename= 让 FileResponse 自动带 Content-Disposition: attachment
    # （区别于 /media/* 播放用的 inline）。
    # 响应发完后删产物；客户端中途断开时 BackgroundTask 不保证跑到，
    # 由 StepExporter._sweep_orphans 兜底。
    return FileResponse(
        path=str(output_path),
        media_type="video/mp4",
        filename=f"task{task_id}_step{step_id}_{track}.mp4",
        background=BackgroundTask(output_path.unlink, missing_ok=True),
    )


# ---------------------------------------------------------------------------
# 接口 3: 健康探测
# ---------------------------------------------------------------------------


@router.get("/health", response_model=LabHealthResponse)
async def lab_health() -> LabHealthResponse:
    """探测 LS 是否配置 + 是否可达 + token 是否有效。

    不抛异常：未配置时 configured=False，可达性判断时 reachable=False + error。
    """
    ls_url = lab_config.get_url()
    ls_token = lab_config.get_token()
    default_pid = lab_config.get_default_project_id()

    if not ls_url or not ls_token:
        return LabHealthResponse(
            configured=False,
            reachable=False,
            error="Label Studio url / token 未配置（url 可在送标页面设置，token 需后端 env）",
            label_studio_url=ls_url or None,
            default_project_id=default_pid,
        )

    reachable, err = await run_in_threadpool(lab_service.ping_label_studio, ls_url, ls_token)
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
    return LabConfigResponse(**lab_config.snapshot())


@router.put("/config", response_model=LabConfigResponse)
async def update_lab_config(req: LabConfigUpdateRequest) -> LabConfigResponse:
    """更新 LS url 与 default_project_id，持久化到文件，重启后保留。

    token 不在此处管理（仅 env）。校验失败抛 400。
    """
    try:
        snap = lab_config.update(
            req.label_studio_url, req.default_project_id, req.task_source
        )
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
