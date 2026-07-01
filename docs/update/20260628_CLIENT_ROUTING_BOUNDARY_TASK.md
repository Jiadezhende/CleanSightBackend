# 换键与路由收敛（T4–T6）：task_id 单键 + 句柄写回

> **变更状态**：实施工单（2026-07-01 修订）
> **知识库**：待落地后沉淀
>
> 契约前置：[20260628_RUNTIME_IDENTITY_BINDING_TASK.md](20260628_RUNTIME_IDENTITY_BINDING_TASK.md)。
> 序列前置：[20260628_CLIENT_QUEUES_LIFECYCLE_TASK.md](20260628_CLIENT_QUEUES_LIFECYCLE_TASK.md)（T1–T3：per-run 不可变 CQ + 状态机 + 单一拆除出口）。

## 工单定位

把写回改成句柄投递（不再按 client_id 反查），把运行时键从 `client_id` 翻成 `task_id`，再迁移对外 wire。承载 **T4 / T5 / T6**——strangler 末段，爆炸面最大，已被 T1–T3 的不可变 CQ + 状态机 + 对象身份 fence 兜底。

## 路由分类原则（取自定稿）

> per-run 组件（Decoder/Actor）构造焊死单个 CQ、不反查；写回**拎捕获的 CQ 句柄**；multi-run Scheduler 动态枚举 registry；纯计算服务不接触 CQ；读当前 run 的（WS/实时告警）按 `task_id` 查。

| 模块 | CQ 获取方式 |
|------|-------------|
| `FFmpegDecoder` / `ClientTemporalActor` | 构造绑定单个 CQ（reconnect 换 decoder 不换 run） |
| `StageAwareDispatcher` | 枚举 registry、pop 时**捕获**该 CQ 句柄塞进 DetectionTask |
| 写回 | 用 DetectionTask/FrameInference 携带的 CQ 句柄，**不反查**，写前查 state |
| `ModelWorkerService` | 不接触 CQ（句柄仅透传） |
| `VisualizationWorker` | 枚举 registry，逐 CQ 读写同对象 |
| WS / 实时告警 / 控制面 | 按 `task_id` 查 registry |

---

## T4 — 写回句柄化（ModelWorker 去 CQ）

**依赖**：T2　**量**：M（1–2d）　**风险**：中（改推理热路径写回）

范围：

- [`StageAwareDispatcher`](../../app/services/inference/detection/dispatcher.py) pop 帧时把该 CQ 句柄捕获进 `DetectionTask`（[`models.py`](../../app/services/inference/models.py)），随 batch 传到 `FrameInference`；
- [`ModelWorkerService._write_back_results`](../../app/services/inference/detection/service.py) 改为 `cq = result.cq`（**不再 `registry.get_client(res.client_id)`**）；写前 `if cq.state==ACTIVE` 否则丢弃并计数（`stale-run`）；FeatureStore 分区键读 `cq.task_id/cq.step_id`；
- ModelWorkerService 不再持 `client_queues_map` / ClientManager，只 `infer_batch → 句柄写回`；
- 迟到结果握旧 CQ 句柄 → 旧 CQ DRAINING/CLOSED（T2）→ 被挡，碰不到新 run。

**ship-green**：句柄与 client_id 并存过渡，写回改用句柄；仍 client_id 键。
**验收**：重启同 `(task,step)` 时 late result 不落新 CQ；ModelWorkerService 无 ClientManager/CQ-map 依赖。

## T5 — 换键 client_id → task_id（ClientManager 降级 RunRegistry）

**依赖**：T3、T4　**量**：L（2–4d）　**风险**：中（call site 多、改测试最多）

范围：

- [`ClientManager`](../../app/services/client/manager.py) → **RunRegistry**：`_clients` 键 `str→int(task_id)`；`get_client` 拆 `find_run(task_id)`（只查）与 `create_run(...)`（仅 start_run）；**删 `bind_task` / `_task_to_client` / `_client_to_task`**；
- **Q2 结构化**：`registry[task_id]` 单槽位天然「一 task 一 run」，删扫描/索引；`get_client_by_task_id` 变直接取值；
- [`InferenceManager._actors`](../../app/services/inference/manager.py)、StreamService `decoders`、`run_lifecycle._teardown_run` 同步换 `task_id` 键；
- 边界（api/ws/task/health）暂加 `source_ip→task_id` 扫描垫片，保住 wire 不变（T6 撤）。

**ship-green**：靠边界垫片，对外行为不变。
**验收**：数据面无 `get_client()` 隐式创建；`registry[task_id]` 单槽位保证 Q2；静态扫描可解释所有剩余 ClientManager 引用；全量 `pytest`（含 run 找不到 / 已关闭 / 已切换）通过。

## T6 — 边界 wire 迁移（需前端协调）

**依赖**：T5　**量**：M（1–2d + 前端联调）　**风险**：外部依赖（前端）

范围：

- 撤 `source_ip→task_id` 垫片；
- [api.py](../../app/routers/api.py)：`terminate` 入参 `client_id → task_id`；`start` 抢占按 task（同 task 换 step/重启）；per-task threading 锁（承接 T3）；
- [ai.py](../../app/routers/ai.py) WS `?client_id=` → `?task_id=`，按 task 查当前 run 的 rendered（切 step 自动跟随）；
- [task.py](../../app/routers/task.py) 实时告警已 task_id，校验贯通；
- [context.py](../../app/utils/context.py) 线程本地标签、health/metrics 由 client_id 改 task_id；
- 删残留 `client_id`/ClientManager import，逐项标注保留引用的合法理由；`client_id` 仅以 `source_ip` 被动字段存活。

**ship-green**：唯一对外破坏性变更，单独发布 + 前端同步。
**验收**：start/terminate/WS 端到端按 task_id 通；切 step 时 WS 自动跟随当前 run。

---

## 非目标

- 不再设计身份/状态机/拆除编排（T1–T3 已定）；
- 不新增第二份 client→CQ 路由表；
- 不让 RunRegistry 代理数据操作或驱动 service；
- 不改 DB 接口；
- 不引入 `run_epoch` 或消息携带身份（判别靠 CQ 对象引用）。

## 完成判据

- 写回按捕获的 CQ 句柄投递，ModelWorkerService 无 CQ/ClientManager 依赖；
- 运行时全量 `task_id` 键；`bind_task`/双向索引移除，Q2 由单槽位结构保证；
- 对外 wire 迁移完成且前端联调通过；
- `client_id` 仅作 `source_ip` 被动字段；
- 全量 `pytest tests/` 通过。
