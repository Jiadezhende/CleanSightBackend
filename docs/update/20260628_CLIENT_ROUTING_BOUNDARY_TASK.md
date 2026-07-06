# 换键与路由收敛（T4–T6）：task_id 单键 + 句柄写回

> **变更状态**：实施工单（2026-07-01 修订）；T4–T5 已落地、T6 暂缓待前端
> **知识库**：待沉淀
>
> 承接：本工单建立在前序重构「CQ per-run 不可变 + 状态机 ACTIVE/DRAINING/CLOSED + 单一拆除出口 + 对象身份 fence」之上——那批把不可变 CQ、写入门、跨 run 隔离就位，本工单在其上换键 + 句柄写回。

## 工单定位

把写回改成句柄投递（不再按 client_id 反查），把运行时键从 `client_id` 翻成 `task_id`，再迁移对外 wire。承载 **T4 / T5 / T6**——strangler 末段，爆炸面最大，已被前序不可变 CQ + 状态机 + 对象身份 fence 兜底。

> 概念：**run** = 一次活跃运行，由**一个 CQ 实例**代表、终生绑 `(task_id, step_id)`；**source_ip** = 被动字段（旧 client_id，非路由键）；判别 run 实例靠 **CQ 对象引用本身**（无 `run_epoch`）。

## 路由分类原则

> per-run 组件（Decoder/Actor）构造焊死单个 CQ、不反查；写回**拎捕获的 CQ 句柄**；multi-run Scheduler 动态枚举 registry；纯计算服务不接触 CQ；读当前 run 的（WS/实时告警）按 `task_id` 查。

| 模块 | CQ 获取方式 |
|------|-------------|
| `FFmpegDecoder` / `ClientTemporalActor` | 构造绑定单个 CQ（reconnect 换 decoder 不换 run） |
| `StageAwareDispatcher` | 枚举 registry、pop 时**捕获**该 CQ 句柄塞进 DetectionTask；stage 由该 cq 派生随行 |
| 写回 | 用 DetectionTask/FrameInference 携带的 CQ 句柄，**不反查**，写前 `is_active()` 统一门控（含 FeatureStore） |
| `ModelWorkerService` | 不接触 CQ（句柄仅透传） |
| `VisualizationWorker` | 枚举 registry，逐 CQ 读写同对象 |
| WS / 实时告警 / 控制面 | 按 `task_id` 查 registry |

---

## T4 — 写回句柄化（ModelWorker 去 CQ）　✅ 已落地（2026-07-02）

**依赖**：T2　**量**：M（1–2d）　**风险**：中（改推理热路径写回）

> 落地（两部分）：
> **(a) 句柄化写回**——`DetectionTask`/`FrameInference` 加 `cq` 句柄字段（[models.py](../../app/services/inference/models.py)，`TYPE_CHECKING` 注解免循环导入）；dispatcher pop 时 `cq=cq` 捕获、pool `cq=req.cq` 透传；[`_write_back_results`](../../app/services/inference/detection/service.py) 改写 `res.cq` + `if not cq.is_active(): frame_drop_total(reason="stale_run").inc(); continue`，删 `has_client`/`get(res.client_id)` 反查与 write-once 的 `client_queues_map` 字段（`_client_manager` 仅留给 Dispatcher 构造）。stage 字段今本已从 cq 派生、保留不动。
> **(b) FeatureStore 归属校验**——feature 腿是唯一 T2 状态门盖不住的（外部落盘 + 分区键 `(task,step)` 跨 run 共享；`is_active()` 到落盘之间状态漂移、`open_fresh` 可插入截断 → 串台）。在 [`_JsonlBuffer`](../../app/services/inference/feature/store.py) 内以 **owner = cq 对象引用** 校验分区归属：`open_fresh(owner=new_cq)` 设、`_enqueue(owner)` 校（不符即拒），owner set/check 与所有文件写/unlink 全在 store `_lock` 下串行（原锁外 `_write` 收进锁内）→ 无 TOCTOU。`append(owner=res.cq)`（[service.py](../../app/services/inference/detection/service.py)）、`open_fresh/close(owner=cq)`（[manager.py](../../app/services/inference/manager.py)）。
> `pytest tests/` **255 passed**，新增 [`tests/test_writeback_handle_fence.py`](../../tests/test_writeback_handle_fence.py)（ACTIVE 写 / DRAINING·CLOSED 三写全挡+计数 / 同 batch 跨 run 隔离）、[`tests/test_feature_store_owner_fence.py`](../../tests/test_feature_store_owner_fence.py)（owner 符落 / supersede 后旧 owner 拒 / None 向后兼容 / close 身份清）。

### 要收口的危险区间（评审 2026-07-02）

热路径上**没有任何消息对象持有 CQ**——[`DetectionTask`](../../app/services/inference/models.py) / [`FrameInference`](../../app/services/inference/models.py) 携带的是 `client_id: str`。CQ 引用只在组件栈上短暂持有，且在**写回处按 `client_id` 反查重取**。清点全流程 CQ 持有区间：

| 持有者 | 获取方式 | 性质 |
|--------|---------|------|
| `StageAwareDispatcher._fetch_and_dispatch_round` | `snapshot()` → `cq.pop_ca_ready()` + `cq.get_stage()`，取完**即弃 cq** | 瞬态、正确 |
| `_write_back_results` | `has_client(res.client_id)` + `get(res.client_id)` **反查** | ⚠️ 缺陷点 |
| `ClientTemporalActor` | 构造注入 `self._cq`（per-run 不变） | 捕获句柄、正确 |
| `VisualizationWorker` | 每 tick `snapshot()` 按 client_id 取，读 latest 槽位 | 瞬态、不跨消息 |
| `FeatureStore.append` | 不持 cq，取写回处派生的 `(task_id, step_id)` | 已解耦 |

危险区间 = **dispatch（弃 cq）→ infer_batch（数十 ms，批量+CUDA sync）→ write-back（按 client_id 反查）**。这段时间内 `set_task`/`stop_run` 可能换槽，写回 `get(res.client_id)` 拿到的是**此刻槽位里的 cq**——可能是新 run 的 cq 或 None。因 `client_id` 是可变路由键、非 run 身份，**旧 run 的迟到结果会写进新 run 的 cq，跨 run 串台**。

### 范围

- [`StageAwareDispatcher`](../../app/services/inference/detection/dispatcher.py) pop 帧时把该 CQ 句柄捕获进 `DetectionTask`（[`models.py`](../../app/services/inference/models.py)），随 batch 传到 `FrameInference`；
- **stage 从捕获的 cq 派生，不再单独存/单独反查**：`DetectionTask.stage` 由 `cq.get_stage()` 取一次随句柄同行，消除 stage 与 cq 身份漂移（避免"cq 是新 run、stage 还是旧"的错配）；
- [`ModelWorkerService._write_back_results`](../../app/services/inference/detection/service.py) 改为 `cq = result.cq`（**删掉 `has_client` + `get(res.client_id)` 两处反查**）；写回前 `if not cq.is_active(): continue` 统一门控整条写回（含 FeatureStore），并计数 `stale-run` 丢弃；FeatureStore 分区键读 `cq.get_task()/cq.step_id_of(task)`（同一 cq 句柄派生，不再跨 `snapshot` 二次读）；
  - 注：`push_detection`/`set_latest_inference` 经 T2 已内建 ACTIVE 门（自动静默拒非活跃写），但 `feature_store.append` 是外部落盘、**不受 cq 门约束**——故写回口显式 `is_active()` 兜住 FeatureStore 这条腿，两者不重复也不遗漏；
- ModelWorkerService 不再持 `client_queues_map` / ClientManager，只 `infer_batch → 句柄写回`；
- 迟到结果握旧 CQ 句柄 → 旧 CQ DRAINING/CLOSED（T2）→ 被挡，碰不到新 run。

**ship-green**：句柄与 client_id 并存过渡，写回改用句柄；仍 client_id 键。
**验收**：重启同 `(task,step)` 时 late result 不落新 CQ（含 FeatureStore 分区不串台）；ModelWorkerService 无 ClientManager/CQ-map 依赖；`DetectionTask.stage` 与其 cq 身份始终一致。

## T5 — 换键 client_id → task_id（ClientManager 降级 RunRegistry）　✅ 已落地（2026-07-04）

**依赖**：T3、T4　**量**：L（2–4d）　**风险**：中（call site 多、改测试最多）

> T5 已落地（2026-07-04），要点：
> 运行键 `source_ip(str) → run_key(str(task_id)) → task_id(int)` 两跳收敛为 **int 单键**（`run_key` 中间态已删，消 str/int 双身份）；
> `ClientManager._runs`/`_task_locks`/`InferenceManager._actors`/`StreamService.decoders`/`HealthMonitor` 全 int 键；
> **`get_client_by_task_id` 已删**（键即 task_id，调用方直接 `get(task_id)` O(1)）；`get_or_create`/`get_result` 死代码清除；
> `CleaningTask` VO 删除，身份 primitives 直挂 CQ；CQ 构造上移 `RunController`，收敛为对称 `start_workflow(cq)`/`stop_workflow(cq)`；
> 内部命名 `client_id→task_id`，诊断/异常字段升级为 `task_id+step_id+source_ip`（响应体 `client_id` wire 键不变、值=source_ip）；
> 边界 `source_ip→run` 垫片（`find_by_source_ip`，匹配首个）保住 wire。`pytest tests/` 264 passed。

范围（原计划，均已落地）：

- [`ClientManager`](../../app/services/client/manager.py) → **RunRegistry**：`_clients` 键 `str→int(task_id)`；`get_client` 拆 `find_run(task_id)`（只查）与 `create_run(...)`（仅 start_run）；**删 `bind_task` / `_task_to_client` / `_client_to_task`**；
- **Q2 结构化**：`registry[task_id]` 单槽位天然「一 task 一 run」，删扫描/索引；`get_client_by_task_id` 变直接取值；
- [`InferenceManager._actors`](../../app/services/inference/manager.py)、StreamService `decoders`、`run_lifecycle._teardown_run` 同步换 `task_id` 键；
- 边界（api/ws/task/health）暂加 `source_ip→task_id` 扫描垫片，保住 wire 不变（T6 撤）。

**ship-green**：靠边界垫片，对外行为不变。
**验收**：数据面无 `get_client()` 隐式创建；`registry[task_id]` 单槽位保证 Q2；静态扫描可解释所有剩余 ClientManager 引用；全量 `pytest`（含 run 找不到 / 已关闭 / 已切换）通过。

### T5 收敛细化：InferenceManager 职责归位 + 告警别名前烧

> 收口 T4 评审记下的「InferenceManager 职责越界」账（病灶复述见下）。
>
> **落地现状（2026-07-03，`refact/lifespan`）**：teardown 归位 + persistence 生命周期上移**已提前落地**（不依赖换键）：
> - 别名前烧：`ClientTemporalActor.__init__` 解析一次 `_stage_alias`，`_tick`/`_collect_settlement_alarms` 产出即烧进 `alarm.stage`；
> - `persist_alarms`/`flush_residual_segments` 迁入 `PersistenceManager`（删 `inference/temporal/alarm_sink.py`），persistence 包零 inference import；
> - InferenceManager 单一 per-run 口 `start_workflow`/`stop_workflow`（原 `set_task`/`remove_client`），删 `_persist_settlement_alarms`/`_flush_all_remaining_segments`/`_client_lifecycle_lock`/`persistence_manager` 引用；
> - `RunController.stop_run` 按序调 `stop_workflow`→`persist_alarms`→`flush_residual_segments`；
> - persistence 生命周期上移 `persistence.lifespan()`，`main.py` 嵌套 health→persistence→ai（起序）/ 逆序停。
>
> **仍随 T5**：CQ 构造/换槽上移 RunController（`start_workflow` 暂仍建 CQ）、client_id→task_id 换键、start 侧收敛为对称 `start_workflow(cq)`。

**病灶复述**：按四角色表 InferenceManager 应仅为推理三池的 **Component owner**（拥有 actor / feature_store 的 start/stop），但当前 [`remove_client`](../../app/services/inference/manager.py) 在替 **持久化** 做腹语——`_flush_all_remaining_segments` 自己 drain CQ、按 `seg_len` 切段、逐块调 `persist_hls_segment`；`_persist_settlement_alarms` 自己 `get_stage_alias` 再调 alarm sink。这些是持久化的领域知识（分段机制、告警 upsert），不该长在推理身上。收敛不是把 persist 调用平移进 RunController，而是**把 teardown-persist 操作还给各真 owner，RunController 只排序**。

**PersistenceManager 进 teardown 是正解，非泄漏**：四角色表「Component owner」行只列 Stream/Inference，是因它框的是 per-run 组件（decoder/actor）；Persistence 是全局 sink、非 per-run 组件，但拆除必然落盘。它是持久化操作的**真 owner**——今天的错是 InferenceManager 越俎替它做。

#### 拆除期 owner 归位（stop_run 按序调各 owner）

| 步骤 | 真 owner | `RunController.stop_run` 调用 |
|------|---------|------------------------------|
| 封闸 | run(CQ) | `cq.to_draining()`（已落地） |
| 停 decoder | Stream | `stream_service.stop_stream(client_id)`（已落地） |
| 停 actor + **交出** settlement | Inference | `settlement = inference_manager.finalize_actor(client_id)` |
| 告警落库 | **Persistence** | `persistence_manager.persist_alarms(settlement, cq=cq, client_id=..., mode="SETTLEMENT")` |
| HLS 残段 drain+切段+写 | **Persistence** | `persistence_manager.flush_residual_segments(cq)` |
| 关 FeatureStore | Inference | `inference_manager.close_feature_store(cq)` |
| close + 出表 | Registry | `registry.remove(..., cleanup=True)`（已落地） |

顺序约束仅一条：`flush_residual_segments` 必须在 `registry.remove(cleanup=True)`（→`cq.close()` 释放帧缓冲）**之前**——现顺序已满足。RunController 新增 `import persistence_manager`（跨域协调居所，已 import stream/inference/client，符合 [run_control.py](../../app/services/run_control.py) docstring 软约定）。

启动侧对称：`set_task` 里「建新 CQ + `client_manager.set` 换槽」上移 `RunController.start_run`（registry 是哑存储、编排归 Controller）；`open_fresh` + 建 actor 留 `inference_manager.start_run_components(cq)`（feature_store 是 inference 自有 L2 组件，归属不变）。

#### 告警别名前烧（decision 1：Persistence 不反向 import inference.naming）

`persist_alarms` 唯一的 inference 耦合是 `get_stage_alias`（[naming.py](../../app/services/inference/naming.py) 的 stage 主键→可读别名）。全仓仅两处 persist 调用（[actor 实时](../../app/services/inference/temporal/actor.py#L119) / [manager 结算](../../app/services/inference/manager.py#L363)），两处的告警**都源出 actor 的 operators**——故别名解析可完全收进 actor：

- **actor 构造期解析一次**：`ClientTemporalActor.__init__` 增 `self._stage_alias = get_stage_alias(stage)`（stage 对本 run 不可变，一次即可；这是全仓**唯一** `get_stage_alias` 告警调用点）；
- **产出即前烧**：`_tick`（实时 judge）与 `_collect_settlement_alarms`（结算 finalize）收集算子告警后，逐条 `alarm.stage = self._stage_alias`（[Alarm.stage](../../app/domain/alarm.py#L49) 默认 `""`，此处填死）。告警离开 inference 前 `.stage` 已是可读别名；
- **persist_alarms 迁入 PersistenceManager**：从 [alarm_sink.py](../../app/services/inference/temporal/alarm_sink.py) 整体搬为 `PersistenceManager.persist_alarms(alarms, *, cq, client_id, mode, log_each=False)`——**删 `stage_name` 参数**，落库直接读 `alarm.stage` / `alarm.metric`（产出方已填）；内部仍 `cq.append_alarm_record_with_gate` → `self.persist_alarm`（gate 在 cq 上、duck-typed，非 inference import）。`alarm_sink.py` 删除，`temporal/__init__` 撤 `persist_alarms` 导出；
- **两调用者复用同一 sink 方法**：实时 = actor 直调 `persistence_manager.persist_alarms(..., mode=REALTIME)`（热路径、非 teardown，不经 RunController）；结算 = RunController 调 `persistence_manager.persist_alarms(settlement, ..., mode="SETTLEMENT")`。一份映射、不再 fork（保住 alarm_sink 当初「实时/结算共用一条映射」的收敛）。

> 别名表本身（[naming.py](../../app/services/inference/naming.py)）不动，可视化叠字仍读它；只有**告警路径**的 alias 解析从两处塌缩进 actor 构造。任何非 actor 的告警产出方（如 task-timeout 若走 `persist_alarms`）须同样在产出处填 `.stage`——persistence 已不再兜底解析。

#### InferenceManager 摘除清单

- 删 `_persist_settlement_alarms`、`_flush_all_remaining_segments`、`persistence_manager` 引用、`self.persistence_manager` 属性；
- `remove_client` → 拆成 `finalize_actor(client_id) -> List[Alarm]`（仅停 actor、return，不 persist）+ `close_feature_store(cq)`；`set_task` → `start_run_components(cq)`（open_fresh + 建 actor），建 CQ/换槽上移 RunController；
- `_client_lifecycle_lock` 摘除（互斥已由 `lock_for(client_id)` per-client 锁在 RunController 承接，T3 已落地）；
- **附带项（归 `refact/lifespan`，本工单只记账）**：`persistence_manager.start()/stop()` 现由 [`InferenceManager.start/stop`](../../app/services/inference/manager.py) 驱动，亦属越界——应上移 lifespan 装配；与本细化正交，不在 T5 强绑。

**ship-green**：随 T5 换键落；行为等价，仅 owner 归位。仍先按 client_id 键，键名随 T5 主体一并翻 task_id。
**验收**：InferenceManager 无 `persistence_manager` / `persist_*` / `_flush_*` 残留；`grep get_stage_alias app/` 告警路径仅剩 actor 构造一处；persistence 包无 `import ...inference...`；结算/实时告警 `stage` 落库值与迁移前逐条一致（别名不变）；HLS 残段落盘帧数与迁移前等价；全量 `pytest tests/` 通过。

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
