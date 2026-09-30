# 删除恒为空的 gpu_oom_total 指标

> **变更状态**：生效中（2026-09-28）
> **知识库**：已沉淀 → [DESIGN_FAULT_TOLERANCE.md](../kb/DESIGN_FAULT_TOLERANCE.md)（2026-09-30）

## 概述

删掉 Prometheus Counter `gpu_oom`（样本名 `gpu_oom_total`）及其全部消费：告警重试里的递增分支、`GET /admin/metrics/json` 的 `gpu_oom_total` 键、admin 页面「GPU OOM」一行、`docs/api/admin.md` 对应字段。核心指标从 5 个减为 4 个。

## 变更背景

- **现状**：唯一递增点在 [`alarm_worker._record_exception`](../../app/services/alarm/alarm_worker.py)——告警上报重试遇到 `ModelInferenceError(is_cuda_error=True)` 时 +1。告警上报链路只调 HTTP 上报接口，永远抛不出推理异常，推理侧也从未埋点，所以该指标自上线起恒为 0。
- **后果**：`/admin/metrics/json` 返回 `gpu_oom_total: 0`（Counter 已注册即出 key），admin 页面常驻一行 0，读者会误以为「GPU 从未 OOM」是观测结论。
- **触发来源**：`persistence → alarm` 改名批次（[20260928_ALARM_RENAME.md](20260928_ALARM_RENAME.md)）内联 GuardedExecutor 时发现；人决定删除而非在推理侧补埋点。

## 方案详情

| 位置 | 改动 |
|------|------|
| `app/services/alarm/alarm_worker.py` | `_record_exception` 只记 `retry_total`；去掉 `ModelInferenceError` import |
| `app/services/utils/metrics.py` | 删 `gpu_oom_total` Counter 与其说明段；段号 5 → 4 |
| `app/routers/admin.py` | `_parse_metrics_json` 删 GPU OOM 段 |
| `app/main.py` | `/metrics` docstring 删该项 |
| `app/static/admin/index.html` | 删「GPU OOM」表格行 |
| `docs/api/admin.md` | 删示例与字段表中的 `gpu_oom_total`；「5 个核心指标」→ 4 个 |
| `tests/test_exception_handling.py` | 删 `test_metrics_gpu_oom` |

`ModelInferenceError` 本身及其 `is_cuda_error` 字段、`main.py` 的异常处理器保留。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `/admin/metrics/json` | 含 `gpu_oom_total: 0` | 无此键 |
| `/metrics` 文本 | 含 `gpu_oom` 的 HELP / TYPE | 无 |
| admin 页面 | 「GPU OOM」行恒 0 | 无此行 |

**自测结果**：全量 `pytest tests/` 845 passed（较前 846 少的 1 条即被删用例）。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 外部 Grafana / 告警规则若引用 `gpu_oom_total` | 查询返回空 | 该序列一直为 0，删除不改变告警结果；有面板引用的话顺手删 |
| 真要监控 GPU OOM | 目前无观测 | 需要时在推理侧（detector 推理调用处）捕获 CUDA OOM 埋点，作为新功能另起 |
