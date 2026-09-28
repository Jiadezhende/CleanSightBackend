# 送标流程、整段导出、LS 探活下沉到 services/lab/service.py（新实现独立落地）

> **变更状态**：生效中（2026-09-28）　新接口已落地并测绿；`routers/lab.py` 仍走旧实现，尚无调用方
> **知识库**：待沉淀

## 概述

新建 [`app/services/lab/service.py`](../../app/services/lab/service.py)（无活体，模块函数）与 [`app/services/lab/types.py`](../../app/services/lab/types.py)，把 `routers/lab.py` 里的送标流程（LS 配置检查 / project_id 解析 / 区间校验 / 剪片上传清理）、整段导出、LS 探活复制成不含 HTTP 概念的服务函数。router 一行未改，迁移是下一步。

## 变更背景

- **现状 / 痛点**：`/lab-f3m8/submit` 的全部业务逻辑（`_validate_clips`、`_resolve_project_id`、`_process_one`、ClipBuilder / LS 客户端构造、job_dir 清理）写在 router 里，返回 pydantic DTO，无法脱离 FastAPI 复用与单测——`/submit` 此前**一条行为测试都没有**。
- **触发来源**：routers 业务逻辑下沉（目标结构见 `20260928_APP_LAYOUT_PROPOSAL.md`：`services/lab/` 承接「校验 → 剪片 → 上传 → 清理」）。
- **承接**：按 DEVELOPMENT §6 四步走的第 1、2 步——新实现独立落地并测绿，旧 router 实现原样保留。

## 方案详情

### 全景：router 迁移后的 `/submit` 调用链

检查顺序是对外契约（503 → 400 → 400 → 404），router 迁移时不得换序：

```text
require_label_studio()                    LabelStudioNotConfiguredError → router 映射成原 503 body
resolve_project_id(req.project_id)        ValidationError(field="project_id") → 400
validate_clips([ClipRange(...), ...])     ValidationError(field="clips") → 400；返回排序后列表
resolve_run + 「raw 轨有段」判定          留在 router → 404（services 不许 import routers）
run_in_threadpool(submit_clips, run, ordered, project_id=, ls_url=, ls_token=,
                  keep_artifacts_on_failure=)   → SubmitOutcome → router 组装 LabSubmitResponse
```

| 部件 | 落在哪 | 对应旧实现（`routers/lab.py` @ 01ba25c） |
|------|--------|------|
| `LabelStudioNotConfiguredError` / `require_label_studio() -> (url, token)` | `service.py` | submit L495-498 + `_ls_not_configured` L772-786（503 body 留 router） |
| `resolve_project_id(requested) -> int` | `service.py` | `_resolve_project_id` L355-364 + L501-503 |
| `validate_clips(clips) -> List[ClipRange]` | `service.py` | `_validate_clips` L302-352 + L504-509 |
| `submit_clips(run, clips, *, project_id, ls_url, ls_token, keep_artifacts_on_failure) -> SubmitOutcome` | `service.py` | submit L520-567 |
| `_process_one` | `service.py` | `_process_one` L572-621 |
| `export_step(run, track) -> Path` | `service.py` | download L650-656 + L665 |
| `ping_label_studio(url, token) -> (bool, Optional[str])` | `service.py` | health `_ping` L721-726 |
| `ClipRange` / `ClipOutcome` / `SubmitOutcome` / `ERR_*` | `types.py` | `LabClipRange` / `LabClipResultDTO` 的业务字段 |

### 1. 与旧实现逐字一致的部分

- 三处 `ValidationError` 文案、`field`、`clip[{i}]` 用**排序后**下标、`Too many clips` 在排序前判——全部照抄。
- error_code 字面量 `range_out_of_bounds` / `range_gap` / `ffmpeg_failed` / `ls_bad_response` 定为 `types.py` 的 `ERR_*` 常量；LS 上传失败时仍优先透传客户端的 `ls_unreachable` / `ls_auth`。
- job_dir：有失败且 `keep_artifacts_on_failure` → 保留并在 `SubmitOutcome.job_dir` 带回；否则 `builder.cleanup`。空列表走清理分支（与旧实现相同）。
- ClipBuilder / LabelStudioClient / StepExporter 的构造参数与 settings 取值逐项相同；ping 超时 10s、异常折成 `"{类型}: {消息}"`。

### 2. 刻意的形状差异（router 迁移时要做的映射）

| 点 | 旧 | 新 | router 怎么接 |
|----|----|----|------|
| 入参区间 | `LabClipRange`（pydantic） | `ClipRange`（NamedTuple） | `[ClipRange(c.start_media_ms, c.end_media_ms) for c in req.clips]` |
| 单段结果 | `LabClipResultDTO` | `ClipOutcome`（dataclass，字段与 DTO 一一同名） | `LabClipResultDTO(**dataclasses.asdict(o))` |
| job_dir | `Optional[str]` | `Optional[Path]` | `str(out.job_dir) if out.job_dir is not None else None` |
| 计数 | router 内算 | 不提供 | `success_count = sum(c.success for c in out.clips)` |
| 上限 | 参数传入 | 函数内读 `settings.lab_export_max_*` | 直接调 |
| default project_id | 参数传入 | 函数内读 `runtime_config` | 直接调 |
| 对象构造时机 | ClipBuilder 在事件循环里构造（mkdir temp_root） | 在 `submit_clips` 内、即线程池里构造 | 无需处理 |
| download 的 `run is None` | 借用 `StepExportNoSegments` 抛出再转 404 | `export_step` 只收非空 run | router 自己判 None 抛 `NotFoundError`，文案保持 `f"No {track} segments for task_id={task_id}, step_id={step_id}"`（与 exporter 的文案不同，别合并） |

### 3. 保留项（不改动）

- `app/routers/lab.py` 全文不动，旧私有函数迁移后单独删。
- `tests/test_lab_clip_builder.py::TestRouterConstructionStaysInSync` 仍读 router 的 AST；router 迁移后把路径改为 `app/services/lab/service.py`（service 里 `ClipBuilder(...)` / `ClipSpec(...)` 保持按名调用 + 关键字参数，该断言可直接套用）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `/submit` 业务逻辑的行为测试 | 0 | 35 条（`tests/test_lab_service.py`，fake ClipBuilder / LS 客户端，不跑 ffmpeg、不连 LS） |
| 导入门禁 | `app.services.lab.*` 未登记 | 登记 `app.services.lab.service`（无重依赖，实测 ~0.17s，上限 0.60s） |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_lab_service.py` | 35 passed |
| `tests/test_import_hygiene.py` | 63 passed |
| 全量 `pytest tests/` | 873 passed, 8 skipped（基线 837 passed, 8 skipped） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| router 尚未切到新实现，两份逻辑并存 | 迁移前改文案/字面量须两处同改 | 下一步 router 迁移（按上文全景的顺序接），再单独提交删旧 |
| `submit_clips` 循环中出现非预期异常（非 `ClipBuildError`）时 job_dir 不清理 | 与旧实现相同，孤儿目录留在 `.lab_exports` | 维持现状，不在本批改行为 |
