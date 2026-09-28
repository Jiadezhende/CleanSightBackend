# lab router 的送标 / 整段下载 / LS 探活切到 services/lab/service.py，删除 router 内旧实现

> **变更状态**：生效中（2026-09-28）
> **知识库**：待沉淀

## 概述

[`app/routers/lab.py`](../../app/routers/lab.py) 的 `POST /submit`、`GET /download`、`GET /health` 改调 [`services/lab/service.py`](../../app/services/lab/service.py)，router 只保留检查顺序、HTTP 错误映射与 DTO 组装；删除 router 里的 `_validate_clips` / `_resolve_project_id` / `_process_one` 及 ClipBuilder / LabelStudioClient / StepExporter 构造。对外响应不变。

## 变更背景

- **现状 / 痛点**：送标流程（LS 配置检查、project_id 解析、区间校验、剪片上传、job_dir 清理）写在 router 里，与已落地的 `services/lab/service.py` 两份并存。
- **承接**：service 新实现已独立落地并测绿（35 条单测）；本批做 DEVELOPMENT §6 的「调用点迁移 + 删旧」。因 router 迁完后旧私有函数即无调用方、且只在 router 一个文件内，迁移与删旧放在同一提交。

## 方案详情

### 全景：`POST /lab-f3m8/submit` 的检查顺序（对外契约，不得换序）

```text
lab_service.require_label_studio()      LabelStudioNotConfiguredError → router 的 _ls_not_configured() 原 503 body
lab_service.resolve_project_id(pid)     ValidationError(field="project_id") → 400
lab_service.validate_clips([ClipRange]) ValidationError(field="clips") → 400；返回排序后列表
resolve_run + hls.query_has_segments    无 run / raw 无段 → 404（resolve_run 属 routers，services 不许 import）
run_in_threadpool(lab_service.submit_clips, run, ordered, project_id=, ls_url=, ls_token=,
                  keep_artifacts_on_failure=)
  → SubmitOutcome → LabClipResultDTO(**asdict(o))；job_dir → str 或 None；success/failure 计数 router 自算
```

| 端点 | 旧（router 内） | 新 |
|------|------|------|
| `/submit` | `_resolve_project_id` / `_validate_clips` / 构造 ClipBuilder + LabelStudioClient / `_do_work` 闭包 / `_process_one` | 上面全景 |
| `/download` | router 构造 `StepExporter`；`run is None` 时借 `StepExportNoSegments` 抛出再转 404 | `run is None` 时 router 直接抛 `NotFoundError`（文案、resource_id 与旧逐字相同，不与 exporter 文案合并）；否则 `run_in_threadpool(lab_service.export_step, run, track)`，异常映射不变 |
| `/health` | 闭包 `_ping` 构造 LabelStudioClient | `run_in_threadpool(lab_service.ping_label_studio, url, token)` |

### 1. 保留项（不改动）

- `_ls_not_configured()` 的 503 body、所有 404 / 500 / 503 文案与 resource_id。
- `/health` 仍由 router 读 `lab_config.get_url/get_token/get_default_project_id`（未配置分支要回显 url 与默认 pid，不走 `require_label_studio`）。
- `LabClipRange` / `LabClipResultDTO` 等 pydantic 模型仍在 router，是 HTTP 契约。

### 2. 测试

- `tests/test_lab_clip_builder.py::TestRouterConstructionStaysInSync` 的 AST 路径改到 `app/services/lab/service.py`（ClipBuilder / ClipSpec 的构造方已下沉）。
- 新增 `tests/test_lab_router_submit.py`（8 条）：`/submit` 的 503 → 400(project_id) → 400(clips) → 404 顺序与错误体、`SubmitOutcome` → 响应映射（排序、kwargs 透传、job_dir、计数）、`/download` 无 run 的 404 且不导出、`/health` 调 `ping_label_studio`。其中 5 条检查顺序 / 错误体用例在旧 router 上同样通过（对拍过），证明行为未变。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| router 行数（wc -l） | 728 | 542 |
| `/submit` / `/download` / `/health` 的 HTTP 层测试 | 0 | 8 |
| 构造 ClipBuilder 的时机 | 事件循环里（会 mkdir temp_root） | `submit_clips` 内、线程池里 |
| `/download` 无 run 时 | 先构造 StepExporter（mkdir `.lab_exports`）再 404 | 直接 404，不建目录；响应相同 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_lab_router_submit.py` + `test_lab_clip_builder.py` | 22 passed |
| 全量 `pytest tests/` | 961 passed, 8 skipped（上一批 953） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `submit_clips` 里非预期异常（非 `ClipBuildError`）时 job_dir 不清理 | 与旧实现相同，孤儿目录留在 `.lab_exports` | 维持现状 |
