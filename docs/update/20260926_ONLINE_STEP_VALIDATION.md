# 在线 `/api/start` 校验 `current_step`：未配置即 400，MOCK 只兜底推理失败

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

在线的 `/api/start` 现在会先校验 `current_step`：非数字，或推理配置（`inference_config.yaml`）里没定义的 step，一律直接返回 400。这一步在动同 task 的旧 run 之前完成，旧 run 不受影响。
MOCK 兜底只剩一种情况：step 配了，但它的 detector 全部加载失败。
影响范围：`RunController.start_run`、`InferenceManager.resolve_stage`，以及集成测试场景 8。

## 变更背景

- **现状**：`resolve_stage` 把所有没进入生效集合的 step 都路由到 MOCK，不区分「YAML 里没配」和「配了但模型加载失败」。非数字的 `current_step` 在 `int()` 处抛 `ValueError`，而 `ValueError` 不属于业务异常，会变成 500。另外，stage 解析放在锁内、重启清理之后，所以一个坏参数会先把同 task 正在跑的旧 run 停掉，然后才失败。
- **定性**：上游正常不会下发未配置的 step，出现就说明系统有问题，应当作参数错误直接失败。MOCK 纯透传（不出框、不告警）只用来兜底推理失败，保证画面不黑屏，不负责掩盖参数错误。
- **连带**：集成测试场景 8 原来用 `current_step="未知阶段"` 去测 MOCK 兜底。这个值其实在 `int()` 处就失败了，场景 8 从来没测到过 MOCK。

## 方案详情

### 全景

```text
/api/start → RunController.start_run(task_id, current_step)
  ① int(current_step)            失败 → ValidationError(field=current_step) → 400
  ② resolve_stage(step_id)
       step 在生效集合（有 detector）   → 恒等返回
       YAML 未定义                    → ValidationError → 400
       YAML 定义了但 detector 全挂      → MOCK（warning 日志）
  ③ lock_for(task_id)：幂等 / 重启清理 → 建 CQ → start_workflow → 起流
```

①② 必须放在 ③ 之前：参数错误不能先把旧 run 拆掉。

| 步骤 | 落在哪 |
|------|--------|
| ① ② 前移到锁外，`ValueError` 换成 `ValidationError` | [`run_control.py`](../../app/services/run_control.py) |
| ② 区分「未配置」和「加载失败」两种情况 | [`InferenceManager.resolve_stage`](../../app/services/inference/online/manager.py) |
| 对外契约新增一条 400 | [docs/api/api.md](../api/api.md) |

### 实现要点

- `resolve_stage` 传入的是解析后的 int `step_id`，不再是原始字符串。这样 `"02"` 这类写法会和存储键 `2` 对应到同一个 stage，不会查不到。
- 判断「YAML 未定义」用的是 `load_stage_config().list_stages()`，也就是 YAML 全集；判断「生效」用的是 `_get_stage_configs()`，只包含有 detector 的 stage。
- 启动不变式保留：MOCK 必须有 detector。它现在服务的是「加载失败」这一种兜底，只改了注释。

### 保留项

- `ClientQueues(stage="MOCK")` 的默认值不动。它只有测试在用，与本次改动无关。
- 离线侧 `InferenceConfig.resolve_stage` 仍是「未配置就回退 MOCK」，下一批（离线作业服务校验）再改。

## 变更效果

| 场景 | 变更前 | 变更后 |
|------|--------|--------|
| `current_step` 非数字 | 500（`ValueError`） | 400，`field=current_step` |
| `current_step` 在 YAML 里未定义 | 静默跑 MOCK 透传 | 400 |
| step 配了但 detector 全部加载失败 | MOCK 透传 | MOCK 透传（不变） |
| 同 task 在跑时，用坏参数重启 | 先停旧 run，再失败 | 旧 run 不受影响 |

**自测结果**

| 项 | 结果 |
|----|------|
| `test_inference_stage_routing.py`：新增未配置 → 400、配了但未生效 → MOCK | 通过 |
| `test_start_rollback.py`：新增非数字 → 400；未配置时不调用 `stop_run` | 通过（改动前后者会失败） |
| 全量 `pytest tests/` | 978 passed |
| 集成测试场景 8（改为断言 400） | 未跑，需要真实后端和 DB |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 上游如果仍有任务的 `current_step` 未配置（例如 DB 默认值 `'0'`） | 这类任务 `/api/start` 会返回 400，没有画面，也不录像 | 已确认上游正常不会下发；出现时按系统问题排查 |
| 「加载失败 → MOCK」只有单测覆盖 | 真实环境的权重加载失败路径没有端到端验证过 | 需要时在 dev 环境挪走权重后，起一个任务验证 |
