# 离线不再兜底 MOCK：作业服务提交即校验，未配置 400，runner 去掉 `strict`

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

离线推理不再兜底 MOCK：step 在推理配置里没定义，或者没配离线模型（`offline` 段为空），一律视为参数错误。
- 作业服务提交时就校验，不通过直接返回 400，不入队，也不留作业记录。
- runner 和 CLI 遇到同样的 step 抛 `ValidationError`，退出码非 0。

与此同时删掉了 `OfflineRunSpec.strict`、CLI 的 `--strict` 参数、`InferenceConfig.resolve_stage`，以及 YAML 里 MOCK stage 下的 `offline` 段。

## 变更背景

- **现状**：离线有两套路由。一套是 CLI 默认用的 `resolve_stage`：未配置的 step 回退到 MOCK 的 `offline`（`BrushRulesSegmenter`）。另一套是作业服务传 `--strict`，未配置就 `skipped`。同一个 step，手动跑和提交作业得到不同结果。另外，`skipped` 混了两种性质不同的情况：参数错（没配置）和数据状态（没有检测结果）。
- **定性**：离线推理失败就是失败。MOCK 只是写在测试 config 里用的测试替身，不应该被任何路由规则自动选中。未配置的 step 属于参数错误，应该在服务边界直接拦下。
- **承接**：在线侧同类改动见 [20260926_ONLINE_STEP_VALIDATION](20260926_ONLINE_STEP_VALIDATION.md)。在线侧依然保留「配了但 detector 全部加载失败 → MOCK 透传」，因为在线要保证画面不黑屏；离线没有这个需求。

## 方案详情

### 全景

```text
admin POST /offline/jobs → OfflineJobService.submit(task_id, step_id)
  ① config.require_offline(step_id)    未定义 / offline 为空 → ValidationError → 400（不入队）
  ② live / 队满检查                    → 409
  ③ 入队 → 子进程 `cli run --json --threads N`
       → OfflineRunner.run：再次 require_offline（CLI 直接调用时的同一道校验）
       → 读检测结果（为空 → skipped）→ 策略 → 写 temporal.jsonl
```

| 部件 | 落在哪 |
|------|--------|
| 唯一的校验规则 `require_offline` | [`InferenceConfig`](../../app/services/inference/config.py) |
| 提交时校验，可注入 `config` 供测试使用 | [`service.py`](../../app/services/inference/offline/service.py) |
| runner 去掉 `strict` 和两个 skipped 分支 | [`runner.py`](../../app/services/inference/offline/runner.py) |
| CLI 删 `--strict` | [`cli.py`](../../app/services/inference/offline/cli.py) |
| admin 页把 400 与 409 一样提示 `detail` | [`index.html`](../../app/static/admin/index.html) |
| 对外契约：新增 400，`skipped` 的含义收窄 | [docs/api/admin.md](../api/admin.md) |

### 实现要点

- 作业服务和 runner 共用 `require_offline`，校验规则只有一处。服务端先校验，是为了让参数错误在提交时就返回 400，而不是排队跑完子进程后才变成 `failed`。
- 作业服务在模块顶层 import `app.services.inference.config`。这个模块只依赖 yaml，不会引入 torch。
- `BrushRulesSegmenter` 保留，作为单测和 smoke test 的测试替身，docstring 已改掉「兜底」的说法。

## 变更效果

| 场景 | 变更前 | 变更后 |
|------|--------|--------|
| admin 提交未配置的 step | 入队 → 子进程 → `skipped` | 直接 400，不入队 |
| admin 提交有在线检测、但没配离线模型的 step（如 step 1） | 入队 → `skipped` | 直接 400 |
| CLI 手动跑未配置的 step | 回退 MOCK，写出 `mock_action` 分段 | 退出码 1，末行 JSON `status=error` |
| `skipped` 的含义 | 没配置 / 没有检测结果 / live | 只剩「没有检测结果」和「live」 |

**自测结果**

| 项 | 结果 |
|----|------|
| `test_offline_pipeline.py`：`require_offline` 各分支、runner 抛错不写、CLI 未配置时退出码 1 | 通过 |
| `test_offline_job_service.py`：未配置 / 无离线模型时提交被拒，不入队不留记录；真实子进程链路（服务放行、子进程读真实 YAML 拒绝 → `failed`） | 通过 |
| `test_admin_offline_jobs.py`：未配置 → 400，`field=step_id` | 通过 |
| 全量 `pytest tests/` | 979 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| admin 表单不会预先过滤出可跑的 step | 用户只能提交后从 400 提示里得知不可跑 | 需要时加一个「可跑 step 列表」端点，暂不做 |
| CLI 的 `--strategy` / `--json` 以及 `StageFactory.override_class` 仍在 | 调用面还没完全精简 | 下一批（CLI 精简）处理 |
