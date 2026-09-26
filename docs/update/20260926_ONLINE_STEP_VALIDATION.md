# 在线 step 校验与去 MOCK：未配置即 400，构造失败启动即失败

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

- **`/api/start` 先校验 `current_step`**：非数字、推理配置（`inference_config.yaml`）里没定义，或者配了但没有在线检测（无 detector），一律返回 400。校验在动同 task 的旧 run 之前完成，旧 run 不受影响。
- **MOCK stage 从生产移除**：不再有兜底 stage。
- **构造失败启动即失败**：detector 或 operator 构造失败时，后端直接启动失败。

影响范围：`RunController.start_run`、`InferenceManager.resolve_stage` / `_get_stage_configs`、`StageFactory`、YAML，以及集成测试场景 8。

## 变更背景

- **现状**：`resolve_stage` 把所有没进入生效集合的 step 都路由到 MOCK 透传，不区分「YAML 里没配」和「配了但 detector 构造失败」。
  - 非数字的 `current_step` 在 `int()` 处抛 `ValueError`，而 `ValueError` 不属于业务异常，会变成 500。
  - stage 解析放在锁内、重启清理之后，所以一个坏参数会先把同 task 正在跑的旧 run 停掉，然后才失败。
  - `StageFactory` 构造 detector / operator 失败时只记日志，stage 会静默少一个流源或少一条规则。
- **定性**：
  - **未配置的 step**：上游正常不会下发，出现就说明系统有问题，当作参数错误直接失败。
  - **MOCK 在生产里没有价值**：YOLO 权重是首次推理时惰性加载的，构造 detector 时不碰权重，所以能让 stage 不生效、进而触发 MOCK 的只有配置写错。真正的推理失败（权重缺失、CUDA 报错等）走的是逐帧降级：该帧 `success=False`、`boxes=[]`，画面照常出，只是没有框；从来不会切到 MOCK。配置错误应该在部署时暴露（fail-fast），而不是用透传掩盖。MOCK 只作为测试替身有用。
- **连带**：集成测试场景 8 原来用 `current_step="未知阶段"` 去测 MOCK 兜底。这个值其实在 `int()` 处就失败了，场景 8 从来没测到过 MOCK。

## 方案详情

### 全景

```text
启动 InferenceManager.start → _get_stage_configs → StageFactory
   任一 detector / operator 构造失败        → 抛 → lifespan 失败，后端起不来
   YAML 里没配 detector 的 stage            → 不生效（不是错误）
/api/start → RunController.start_run(task_id, current_step)
  ① int(current_step)                        失败 → ValidationError(field=current_step) → 400
  ② resolve_stage(step_id)
       在生效集合（有 detector）              → 恒等返回
       YAML 未定义 / 定义了但没 detector      → ValidationError → 400（无兜底分支）
  ③ lock_for(task_id)：幂等 / 重启清理 → 建 CQ → start_workflow → 起流
运行时推理异常 → 逐帧降级（success=False、boxes=[]），画面照常
```

①② 必须放在 ③ 之前：参数错误不能先把旧 run 拆掉。

| 部件 | 落在哪 |
|------|--------|
| ① ② 前移到锁外，`ValueError` 换成 `ValidationError` | [`run_control.py`](../../app/services/run_control.py) |
| ② 去掉 MOCK 分支，分「未定义」和「无在线检测」两种报错 | [`InferenceManager.resolve_stage`](../../app/services/inference/online/manager.py) |
| 构造失败直接抛 | [`StageFactory.create_detectors_for_stage` / `create_operators_for_stage`](../../app/services/inference/stage_factory.py) |
| 删 MOCK stage、`FALLBACK_STAGE` 以及「MOCK 必须生效」的启动检查 | `config/inference_config.yaml`、[`config.py`](../../app/services/inference/config.py)、`manager.py` |
| 对外契约新增一条 400 | [docs/api/api.md](../api/api.md) |

### 实现要点

- `resolve_stage` 传入的是解析后的 int `step_id`，不再是原始字符串。这样 `"02"` 这类写法会和存储键 `2` 对应到同一个 stage。
- 判断「YAML 未定义」用的是 `load_stage_config().list_stages()`，也就是 YAML 全集；判断「生效」用的是 `_get_stage_configs()`，只包含有 detector 的 stage。
- 构造失败会被 `_get_stage_configs` 外层包成 `RuntimeError`，冒到 `inference.lifespan`，后端启动失败。推理子进程在启动时构造 detector 走的是同一段代码，但主进程会先失败，子进程不会带着坏配置起来。
- `ClientQueues` 的 `stage` 默认值从 `"MOCK"` 改成 `""`，和 `task_id`、`step_id` 等身份字段的裸建默认值一致。生产里 CQ 只由 `RunController` 构造，并且总会显式传入 stage。
- MOCK 实现移出生产代码：`MockDetector` 与离线的 `BrushRulesSegmenter` 挪到 [`tests/doubles.py`](../../tests/doubles.py)（测试 config 用 `doubles.Xxx` 引用）；`MockOperator` 没有测试引用，直接删；`AlarmType.MOCK` 删除，测试改用 `PROCESS_VIOLATION`。infer-workflow skill 的模板 B 参考改指向 `tests/doubles.py`。

## 变更效果

| 场景 | 变更前 | 变更后 |
|------|--------|--------|
| `current_step` 非数字 | 500（`ValueError`） | 400，`field=current_step` |
| `current_step` 在 YAML 里未定义 | 静默跑 MOCK 透传 | 400 |
| step 在 YAML 里有，但没配 detector | 静默跑 MOCK 透传 | 400（「未配置在线检测」） |
| detector / operator 构造失败（配置错误） | 记日志，stage 少一个组件或整体走 MOCK | 后端启动失败 |
| 运行时推理失败 | 逐帧降级，画面照常 | 不变 |
| 同 task 在跑时，用坏参数重启 | 先停旧 run，再失败 | 旧 run 不受影响 |

**自测结果**

| 项 | 结果 |
|----|------|
| `test_inference_stage_routing.py`：未定义 / 无 detector → 400；构造失败 → `_get_stage_configs` 抛；无 detector 的 stage 不生效但不报错 | 通过 |
| `test_offline_pipeline.py`：真实 `StageFactory` 遇到坏的 detector class、rule 缺 subscribes 时直接抛 | 通过 |
| `test_start_rollback.py`：非数字 → 400；未配置时不调用 `stop_run` | 通过（改动前后者会失败） |
| 真实配置构造冒烟（`InferenceManager()._get_stage_configs()`） | `['1', '2']`，不抛异常 |
| 全量 `pytest tests/` | 977 passed |
| 集成测试场景 8（改为断言 400） | 未跑，需要真实后端和 DB |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 上游如果仍有任务的 `current_step` 未配置（例如 DB 默认值 `'0'`） | 这类任务 `/api/start` 返回 400，没有画面，也不录像 | 已确认上游正常不会下发；出现时按系统问题排查 |
| 一个 stage 的配置写错，整个后端都起不来 | 其它 stage 也用不了 | 有意为之（方案 A，配置错误 fail-fast）；部署后看启动日志 |
