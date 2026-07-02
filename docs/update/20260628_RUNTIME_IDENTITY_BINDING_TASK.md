# 运行身份与绑定关系建模任务：厘清 client / task / step / run

> **变更状态**：T1/T2/T3/T4 已落地（2026-07-02）　<!-- 契约已拍板；本文件是落地路线的契约 hub -->
> **知识库**：T4 写回句柄化（dispatcher 捕获 CQ 句柄，写回 res.cq 不反查）+ FeatureStore 归属校验（store 内以 cq 对象引用为 owner，锁下串行 set/check/落盘，闭合 supersede TOCTOU）已落地，可沉淀
>
> 落地现状见文末 [§落地现状（2026-07-02）](#落地现状2026-07-02)。
>
> 工单：[CQ 生命周期与拆除编排（T1–T3）](20260628_CLIENT_QUEUES_LIFECYCLE_TASK.md)、[换键与路由收敛（T4–T6）](20260628_CLIENT_ROUTING_BOUNDARY_TASK.md)。
> [消息上下文传播](20260628_MESSAGE_CONTEXT_PROPAGATION_TASK.md) 已被本轮方案吸收，标记为 superseded。
>
> 相关：[20260628_DATAMODEL_LAYERING.md](20260628_DATAMODEL_LAYERING.md)、[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)。

## 任务定位

定义运行身份、唯一性不变量、绑定关系的权威拥有者，以及运行时组件的拆除编排职责。它是后两份工单的输入契约。

## 当前问题

系统同时使用三种标识：

| 标识 | 当前来源 | 当前用途 |
|------|---------|---------|
| `client_id` | `/api/start` 取 `DBTask.source_ip` | ClientManager/CQ/Decoder/Actor/WS/Health 的运行时路由键 |
| `task_id` | `clean_task.task_id` | 业务查询、告警归属、ClientManager 反向索引 |
| `step_id` | `int(clean_task.current_step)` | inference stage 主键、HLS/FeatureStore/FactLedger 分区 |

持久化侧以 `(task_id, step_id)` 为主；运行态以 `client_id` 为主；两套键靠 `bind_task` 双向索引 + `cq.task` 临时搭桥，没有显式 active-run 模型。

- `bind_task` 的 `_task_to_client`/`_client_to_task` 对「一 task 绑两 client」会自相矛盾；
- `client_id == source_ip`，业务切洗消台/step 时会被覆写，不是可靠身份；
- CQ 跨 run 复用（`set_task` 原地改 `task` + `clear_task_caches`），一个 CQ 实例服务多次运行，迟到写入无栅栏。

---

## 决策结论（2026-07-01 定稿）

**核心：运行时以 `task_id` 单键索引一个 CQ；CQ 即 run 对象，持不可变 `(task_id, step_id, source_ip)`；判别 run 实例靠 CQ 对象引用本身，不需要 `run_epoch`，也不需要消息携带身份。** 多数结论是把「代码已经在做的」扶正，并借三条原则（切 step = 新 run、禁止重跑、cq per-run 不可变）消掉中间层。

### 概念定义

- **task**：业务任务（`clean_task.task_id`）。
- **step**：清洗阶段（`int(current_step)`），stage 路由 + 存储分区。
- **run**：一次活跃运行，**由一个 CQ 实例代表**，终生绑定一个 `(task_id, step_id)`，不换 step。
- **source_ip**：被动字段（旧 client_id，流来源/诊断），**非路由键、不承担互斥**；同一 source_ip 是否并发由业务后端负责。

### 绑定关系（反转 + 单键）

```text
registry: task_id → CQ          （唯一注册表；一个 task 一个槽位 = Q2 结构化）
CQ 持不可变 (task_id, step_id, source_ip) + 可变 status
CQ 拥有/可达其下的 decoder、actor（可替换资源，reconnect 时换 decoder 不换 run）

运行时键 = task_id          存储键 = (task_id, step_id)   （由 CQ 内 step 派生）
task_id 1──1 active CQ      (task_id, step_id) 1──1 active run
source_ip N──N task         （无约束）
```

> 反转要点：今天是 `cq.task`（CQ 拥有 task）；目标是 **run（CQ）拥有身份**，`cq.task` 可变字段下线，身份构造时注入且不可变。

### 逐问结论

| # | 问题 | 决策 |
|---|------|------|
| 1 | client 是什么 | **概念取消**；运行键 = `task_id`，source_ip 仅被动字段 |
| 2 | 一 task 一活跃 run | **是**，`registry[task_id]` 一个槽位 = 结构性不变式（无需扫描/索引） |
| 3 | 一 `(task_id, step_id)` 一活跃 run | **是** |
| 4 | 切 step | **新 run**（旧 CQ 拆除、新 CQ 建立） |
| 5 | 运行代际 | **不需要**。切 step 换 `(task,step)`；同键复用（重启）由 **CQ 对象引用**区分，无需 `run_epoch` |
| 6 | active binding 真源 | 进程内 `registry: task_id → CQ`；删 `bind_task` 与双向索引；**不放 DB** |
| 7 | 冲突 | 同 task 全同参→幂等；改 url→原地重启 decoder（run 不变）；换 step→抢占旧 run；不同 task 共用 source_ip→互不干涉 |
| 8 | 查询键 | 控制面/WS/terminate/实时告警 → **`task_id`**；运行时内部/落盘 → CQ 内 `(task_id, step_id)`；source_ip 不作查询键 |

### 判别 run 实例 = CQ 对象引用（替代 run_epoch）

`run_epoch` 曾用于区分「同键被重启复用的不同 run 实例」。改为**以 CQ 对象引用本身作判别物**后不再需要：

- **写回拎捕获的 CQ 句柄，绝不 `registry[task_id]` 反查**——迟到结果写回旧 CQ 对象，被其状态机（DRAINING/CLOSED）挡掉，碰不到新 CQ；
- **注销用对象身份核对（仅 HealthMonitor 路径需要）**——拆除仅当 `registry[key] is cq_captured` 才动手。用户 `start`/`terminate` 在**同一次持锁内决策+执行**（handler 拿锁后才比对/拆除），无 ABA，普通 `remove` 即可；**唯 HealthMonitor 自动结束**在 monitor 线程「先决策后拿锁」有窗口——决策→拿锁之间可能被 `/start` 重启换槽，共用锁只串行化执行、**不作废过期决策**，故需对象身份核对**防 HM 过期决策误删健康新 run**。捕获点 = `_enter_reconnect_mode` 进入重连时把 cq_A 存进 `ReconnectState`（**非** give-up 时刻——那时 `snapshot()` 拿的是当前槽位 cq，若已换成 B 会误捕 cq_B）；每 tick 比对当前 cq 与存的 cq_A，不同即弃掉本次重连，attempt 耗尽 → `stop_run(expected=cq_A)`；
- **状态机不替代 fence**：状态机门写不门拆除；拆掉健康新 run 是控制面误动作，只有对象身份核对拦得住。两者正交——**状态机**隔离同 run 拆除期的迟到写、**对象身份**隔离跨 run 的过期拆除。

根因：**per-run 不可变 CQ（T1）**才是真药——CQ 不再可变复用后，从句柄读 `cq.step_id` 完全安全，`run_epoch` 与「消息携带身份」都成了多余备份。

### 重启策略

**禁止重跑**：同 `(task_id, step_id)` 重启视为 supersede——新 run 启动时**覆盖/截断该存储分区**（facts 用 `replace_segments`，features open-fresh）。不引入 `{task}/{step}/{run}` 存储层级。

---

## 四角色职责

| 角色 | 谁 | 职责 | 不做 |
|------|----|----|------|
| **Registry** | ClientManager（COW，暂 client_id 键）→ RunRegistry | `key→CQ` 哑存储、`set`/`remove`/`remove_if`、枚举 | 不绑定、不编排、不调别的 service |
| **Orchestrator** | **RunController**（`app/services/run_control.py`，已落地） | 对称 `start_run` / `stop_run` 固定顺序，`client_manager.lock_for(client_id)` per-client RLock（api 经 `asyncio.to_thread`、HM 直调，共用同锁重入） | 不做健康探测 |
| **Health custodian** | HealthMonitor | reconnect 重试/修复；**重连耗尽 → 调 `stop_run` 自动结束**（决策时捕获 cq_A，`stop_run(expected=cq_A)` 防过期误删）；孤儿回收；资源兜底 | 不自己手写拆除步骤 |
| **Component owner** | StreamService / InferenceManager | 拥有并 start/stop/respawn 自己的 decoder/actor | 不被多处 cleanup 各自乱调 |

> 全局池（ModelWorkerService / VisualizationPool）非 per-run，一次 run 结束不停它们，cq 一摘自然不再服务。

### `_teardown_run` 单一出口

```text
_teardown_run(task_id, expected=cq):   # api 控制面，持 per-task threading 锁
  若 registry[task_id] is not cq: no-op        # 对象身份 fence
  cq → DRAINING                                 # ① 封闸，写入被 gate
  StreamService.stop_stream(task_id)            # ② 停生产者（异步停无妨）
  actor.finalize_and_stop()                     # ③ 停分析者 + settlement（DRAINING 允许）
  flush 残余 HLS / feature.close                # ④ 落盘收尾
  cq.close()                                    # ⑤ 释放重数据留壳
  registry.remove(task_id, expected=cq)         # ⑥ 对象身份核对后出表
```

三个调用者**共用同锁**：用户 `terminate`、用户 `start` 重启（先拆旧）、**HealthMonitor 重连耗尽自动结束**（仅运行态回收，不发 alarm、不写 DB 终态）。`async` 端点经 `asyncio.to_thread` 卸载，避免阻塞事件循环。**fence 仅 HM 路径需要**——用户两路在同锁内决策+执行、无 ABA，用普通 `remove`；HM 异步「先决策后拿锁」有窗口，用 `remove_if(expected=cq_A)`。已合并原先重复的 `HealthMonitor.cleanup_client` 与 `api` 手动兜底两份实现为 `RunController.stop_run` 单一出口。

> 上方为目标形态（task_id 键、单一出口）；当前已落地为 `stop_run(client_id, ...)` + `lock_for`，DRAINING 置位与 `remove_if` fence 为收尾项，详见 [§落地现状](#落地现状2026-07-02)。

---

## 落地路线（T1–T6 strangler 序列）

按 **加固内部 → 换键 → 换 wire** 推进，每步独立 merge 且测试全绿；换键（T5）被推到身份/状态机/编排就位之后。

| # | 任务 | 文档 | 依赖 | 量 | 状态 |
|---|------|------|------|----|------|
| T1 | CQ per-run 不可变化（构造注入身份，下线原地复用） | CQ 生命周期 | — | L | ✅ 已落地 |
| T2 | CQ 状态机 ACTIVE/DRAINING/CLOSED + 写入门 + close() 释放重数据 | CQ 生命周期 | T1 | L | ✅ 已落地（纯 queues.py，门由单测覆盖） |
| T3 | `stop_run` 单一出口 + per-client `lock_for` 锁 + 合并两份重复 + HealthMonitor 自动结束 | CQ 生命周期 | T2 | M/L | ✅ 已落地（编排 + 收尾：DRAINING 置位 + HM 对象身份 fence） |
| T4 | 写回句柄化（dispatcher 捕获 CQ 句柄，写回不反查，ModelWorker 去 CQ） | 换键与路由 | T2 | M | ✅ 已落地（2026-07-02，见 [§T4 评审](#t4-评审2026-07-02)） |
| T5 | 换键 `client_id→task_id`，ClientManager 降级 RunRegistry，Q2 结构化（bind_task/双向索引已由 COW 重构提前删除） | 换键与路由 | T3, T4 | L | ⬜ 未开始 |
| T6 | 边界 wire（WS/terminate 改 task_id，撤垫片，import 审计）需前端协调 | 换键与路由 | T5 | M | ⬜ 未开始 |

依赖图：

```text
T1 → T2 → ┬→ T3 ──────┐
          └→ T4 ───────┴→ T5 → T6（前端）
```

T3 与 T4 都只依赖 T2、可并行；T5 需两者到位。合计约 **12–18 人日**（不含前端联调与回归）。

## 落地现状（2026-07-02）

**T1 — CQ per-run 不可变（✅ 已落地）**
- `ClientQueues` 构造注入不可变 `(task_id, step_id, source_ip, stage)`；删 `set_task`/`set_stage`/`clear_task_caches`；身份 getter 免锁直读；`append_ca_*` 读 `self.step_id`；`clear()` 只释放 payload、不重置身份。保留 `task=None` 供纯队列单测裸建。
- `InferenceManager.set_task` 建**新** CQ → `ClientManager.set(client_id, cq)` 换槽 → `FeatureStore.open_fresh` 截断分区（重启 supersede）→ 绑 actor。旧 run settlement 落到捕获的 `old_cq`（**排序不变式消解**，「actor 固化身份」并入 T1）。（注：FactLedger 是离线异步写，生命周期归离线 runner，已从在线 manager 摘除，不再随 set_task open_fresh。）
- 配置单一出口 `ClientConfig.cq_kwargs()`（修 dead-kwargs 潜伏 bug）；`StreamService._get_client_queues` 只取不建。
- 文件：`app/services/client/{queues,manager,config}.py`、`app/services/inference/manager.py`、`app/services/inference/feature/store.py`、`app/services/stream/service.py`。

**T2 — CQ 状态机 + 写门 + close()（✅ 已落地，纯 queues.py）**
- `RunState` ACTIVE/DRAINING/CLOSED + `_state_lock`（概念 0 号锁，不与 7 把 payload 锁互嵌）；`to_draining`/`close`/`is_active`/`get_state`。
- 写时门控：帧/推理/检测写非 ACTIVE 拒；rendered/temporal 拒非空写、放行清空（拆除期清残帧）；alarm 非对称（仅 CLOSED 拒，DRAINING 放行 settlement）。
- `close()` 释放 payload 留身份小壳；`clear()` → `close()` 别名（ClientManager teardown 零改动获内存回收 + CLOSED 门）。
- **本批未接生产 DRAINING**：门由单测覆盖。`stop_run` 现走 `remove`（=`clear`=`close`），故 **CLOSED 门在生产已生效**（拆除后旧 CQ 转 CLOSED，持旧句柄者迟到写被拒）；**DRAINING 态尚未在生产触发** → 见 T3 收尾。

**T3 — 编排单一出口 + 收尾（✅ 已落地）**
- 编排：`RunController.start_run`/`stop_run` 对称固定顺序、`client_manager.lock_for(client_id)` per-client RLock 共用（api 经 `asyncio.to_thread`、HM 直调、同线程可重入）、api `/start`·`/terminate` 委托、`HealthMonitor.cleanup_client` → `stop_run`。仍 client_id 键（换 task_id 键留 T5）。
- 收尾 1 — **DRAINING 置位**：`stop_run` step 0b 调 `cq.to_draining()`（在停 decoder/落盘之前），让 T2 的 DRAINING 门在生产链路真正触发——拆除期停生产者写（decoder 抽帧/结果写回/tick 被门拒），放行 settlement 告警 + HLS flush。CLOSED 由 step 3 `remove(cleanup=True)`→`clear()`→`close()` 兜底。
- 收尾 2 — **HM 路径对象身份 fence**：`ReconnectState` 增 `cq` 字段，`_enter_reconnect_mode(client_id, last_frame_time, cq)` 捕获 cq_A；`_handle_reconnecting_client` 每 tick 比对当前槽位 cq，异即弃本次重连；`_exit_reconnect_mode` 从 state 取 cq_A 透传 → `_cleanup_failed_client`/`cleanup_client`（新增 `expected`）→ `stop_run(expected=cq_A)`。`stop_run` step 0 核对 `registry[client_id] is expected`，不符则整段放弃（`skipped=True`），step 3 用 `remove_if(client_id, expected)`；task-timeout / orphan 路径亦传 `expected=cq`。用户 `start`/`terminate` 不传 `expected`（同锁内决策+执行，无 ABA，走普通 `remove`）。
- 验证：`pytest tests/` **247 passed**（242 基线 + 3 teardown fence + 2 reconnect fence），`import app.main` 无循环导入。新增 `tests/test_teardown_identity_fence.py`、`tests/test_reconnect_on_initial_failure.py::TestReconnectIdentityFence`。

---

## T4 评审（2026-07-02，✅ 已落地）

对 T4「写回句柄化」的落地前评审 + 落地记录，条款见 [换键与路由 §T4](20260628_CLIENT_ROUTING_BOUNDARY_TASK.md#t4--写回句柄化modelworker-去-cq)。落地：`DetectionTask`/`FrameInference` 加 `cq` 句柄字段（dispatcher 捕获、pool 透传）；`_write_back_results` 改写 `res.cq` + `is_active()` 统一门 + `frame_drop_total(reason="stale_run")` 计数，删 `has_client`/`get(res.client_id)` 反查与 `client_queues_map` 字段；`pytest tests/` **251 passed**（247+4），新增 `tests/test_writeback_handle_fence.py`。评审要点：

**1. 消息不携带身份，携带 `client_id`——反查是唯一缺陷点。** 热路径 `DetectionTask`/`FrameInference` 只带 `client_id: str`；CQ 仅在组件栈上瞬态持有。危险区间 = **dispatch（弃 cq）→ `infer_batch`（数十 ms）→ write-back（按 client_id 反查）**：期间换槽则 `get(client_id)` 命中新 run 的 cq，旧结果串台。修法即定稿 line 77——写回拎捕获的 CQ 句柄，绝不反查；旧句柄经 T2 状态机门自然被挡。

**2. stage 随 cq 派生。** 捕获 cq 后 `DetectionTask.stage` 从 `cq.get_stage()` 取一次同行，消除 stage 与 cq 身份漂移；`dispatcher` 里对 cq 的 `get_stage()` 不再是"取完即弃"的孤读。

**3. FeatureStore 腿需 store 内归属校验（`is_active()` 不够）。** `push_detection`/`set_latest_inference` 经 T2 已内建 ACTIVE 门；但 `feature_store.append` 外部落盘不受 cq 门约束，且**贴 `is_active()` 也挡不住**——check 到真正落盘之间状态会漂移，`open_fresh`（新 run）能挤进窗口截断分区，迟到帧落进新 run 文件 = 串台。根因不对称：CQ 两腿迟到写落**旧 cq payload**（per-run 丢弃，无害），feature 腿写 **`(task_id, step_id)` 文件**（restart-supersede 跨 run 共享，有害）。修法：在 `_JsonlBuffer` 内以 **owner = cq 对象引用** 做归属校验——`open_fresh(owner=new_cq)` 设、`_enqueue(owner)` 校（不符即拒），且 owner set/check 与所有文件写/unlink **全在 store `_lock` 下串行**（把原锁外 `_write` 收进锁内），彻底无 TOCTOU。写回 `append(owner=cq)`、`close(owner=cq)` 按身份核对清 owner。顶层 `is_active()` 保留做常态早退+计数，与 store 内归属校验互补。已落地：`pytest tests/` **255 passed**（+4），新增 `tests/test_feature_store_owner_fence.py`。

**4. InferenceManager 职责越界（超出 T4，记账留 T3/T5 收敛）。** 按四角色表 InferenceManager 应仅为推理三池（ModelWorker/VizPool/Actor）的 **Component owner**，但当前 `set_task`/`remove_client` 越界做了 run 编排 + 持久化：建 CQ 换 registry 槽、`open_fresh`/`close` 存储分区、`_flush_all_remaining_segments` 直调 `persistence_manager.persist_hls_segment`、`_persist_settlement_alarms` 落告警。收敛方向——建 CQ/换槽/open_fresh 上移 `RunController.start_run`，HLS flush/feature close/告警落盘由 `_teardown_run` 单一出口按序调各 owner，InferenceManager 退化为「停 actor + 交出 settlement 列表」，`_client_lifecycle_lock`、`persistence_manager` 引用、`_flush_all_remaining_segments` 随之从其身上摘除。此项不属 T4 范围，随 T5 换键一并落——落地细化（owner 归位表 + 告警别名前烧「Persistence 不反向 import inference.naming」+ 摘除清单）见 [换键与路由 §T5 收敛细化](20260628_CLIENT_ROUTING_BOUNDARY_TASK.md#t5-收敛细化inferencemanager-职责归位--告警别名前烧)。

## 完成判据

- 运行时全量按 `task_id` 键，CQ 持不可变 `(task_id, step_id, source_ip)`；
- `bind_task` / 双向索引移除，Q2 由单槽位结构保证；
- 迟到写入被 CQ 状态机 + 对象身份双重挡住，无跨 run 串台；
- 拆除只有一个 `_teardown_run` 出口，三调用者共用同锁；
- HealthMonitor 保留 reconnect/孤儿/兜底，重连耗尽自动结束走同一出口；
- 全量 `pytest tests/` 通过。
