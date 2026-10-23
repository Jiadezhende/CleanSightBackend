# 新增 session 模块：run 注册表 + 按 run 隔离的进程内 mq，替代 client 包与 ClientQueues

> **变更状态**：已落地（2026-10-24）——批 0 见 [20261023_RUN_LIFECYCLE_FIXES](20261023_RUN_LIFECYCLE_FIXES.md)；批 1–8 各一份记录（`20261024_SESSION_*.md`），与本提案的偏差写在各批记录里
> **知识库**：无需沉淀（提案本身；结论随各批记录沉淀）

## 概述

新建 `app/services/session/`：唯一的 run 注册表（`task_id → Session`）+ 进程内 mq 基建。`Session` 只含不可变身份、状态机与 topic 字典；
具体 topic 由各模块在自己的 `topics.py` 声明，经 `sess.channel(TOPIC)` 读写。`ClientQueues`（CQ）的数据槽位迁成 topic，client 包最终删除。
运行时行为、对外接口、日志与指标名不变。本文只定任务与设计决策，实现细节进各批记录。

## 变更背景

- **现状**：在线链路四段（送帧、推理写回、时序、渲染）各自一个时钟，经 CQ 上的槽位交接。CQ 同时承担身份、状态机、10 个槽位、6 把锁、告警去重、启动埋点、压力上报、抽帧计数；`client_service` 是 run 注册表。
- **承接**：批 0 已把生命周期写统一到 run_control（health_monitor 只决策、带 fence 执行）、recording 取帧锁串行三条取帧路径、删除挂起 flush 机制。本提案在此之上换掉 CQ。

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | 推理链路改语义要改 client 包：`client/queues.py` 自 2026-07 起 37 次提交，约 10 次源于推理侧 | client 包装着滑窗 / 时序事件等推理概念 |
| #2 | 约 10 个写方法各自手写 `if self._state is not RunState.ACTIVE`，3 种变体，检查在锁外 | 新槽位漏写写门 = 旧 run 迟到写串入；锁外检查留有「清空后又追加一帧」的窗口 |
| #3 | 释放时按固定顺序同持 6 把锁 | 加锁要记全局顺序 |
| #4 | 拓扑隐式；加一段处理要改 CQ：字段 + 锁 + 2–4 方法 + 写门 + 丢帧 + 压力 + `clear` + `get_queue_depths` | 维护成本 |
| #5 | viz `_last_rendered_ts`、health_monitor `_reconnecting_clients` 是服务内的 task_id 表 | 每 run 状态脱离 run 生命周期；`/terminate` 正在重连的 run 后条目残留 |

## 方案详情

### 全景：结构与生命周期

```text
session_service（唯一的 run 注册表）
└─ Session
   ├─ info   不可变身份（run、source_ip、stage、started_at）
   ├─ state  ACTIVE → DRAINING → CLOSED（只由 run_control 经 session_service 切换）
   └─ topic 字典：首次 sess.channel(T) 时按声明创建本 run 的实例，close 时整体释放

start_run（持任务锁）  open → 起推理 workflow → 起流；任一步失败 → stop_run
stop_run （持任务锁）  seal（拒写，读照常）→ 停 decoder → 停 workflow（收结算告警）
                       → recording 交出残帧 → close（摘出注册表、释放全部数据）
health_monitor        只做决策；执行经 run_control，携带决策时捕获的 sess 作 fence
写方                  构造时或取到工作时捕获 sess，之后只对它读写；查表只在入口
```

硬约束：

- 一个 task 至多一个注册中的 `Session`；换代必须先走完 `stop_run`。
- 生命周期写（open / seal / close、decoder 启停与重启、actor 增删）只在 run_control 持任务锁时发生。
- 写入只在 ACTIVE 放行；读取与清空任何状态放行。写门与状态切换同锁。
- 残帧必须在 `close` 之前交出。
- 基建不认识业务名词（段、档位名、告警 key）；带业务概念的参数由声明方或配置组装方算好再传入。

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| mq 基建：三种语义、`Session` | `session/core/` | 块 A |
| 注册表 | `session/service.py` | 块 A、C |
| topic 声明 | `session/topics.py`、`inference/online/topics.py`、`daemons/health_monitor/topics.py` | 任务清单 |
| 锁序约定 | `docs/DEVELOPMENT.md` | 块 B |
| 任务锁、生命周期编排 | `run_control` | 块 C |

### 任务清单

| 任务 | 内容 |
|------|------|
| T1 建 session 包 | `core/`：Latest / Fifo / TimeWindow 三种语义与 `Session`；`service.py`：注册表 |
| T2 槽位映射为 topic | 见下表 |
| T3 非 topic 化的收尾 | 告警账本原样抽成 `AlarmLedger`，挂 `alarm_sink` 的弱引用表；启动埋点由生产方自行判断首次；抽帧计数器移入 decoder |
| T4 锁序约定与盘点 | DEVELOPMENT.md 写三层锁序与锁表；盘点 `app/` 现有锁并归层，违规嵌套单独上报修复 |
| T5 迁移调用点 | client 包业务调用约 60 处、测试约 100 处，按批迁移 |
| T6 收尾删除 | 删 client 包与 CQ；配置读取平移；导入门禁；DEVELOPMENT.md §3 改为「跨服务运行数据走 topic」 |

| topic | 语义 | 写方 | 读方 | 替代 | 声明位置 |
|---|---|---|---|---|---|
| READY | Fifo（满拒新） | decoder | dispatcher | `ca_ready` | `session/topics.py` |
| RAW | Fifo（满挤旧） | decoder | recording | `ca_raw` | `session/topics.py` |
| LATEST_RAW | Latest | decoder | viz、health_monitor | `latest_raw_frame` / `latest_raw_timestamp` | `session/topics.py` |
| DETECTIONS | Fifo（满挤旧） | 写回 | recording | `ca_detections` | `session/topics.py` |
| PROCESSED | Fifo（满挤旧） | viz | recording | `ca_processed` | `session/topics.py` |
| RENDERED | Latest | viz | `routers/ai.py` WS | `_latest_rendered` | `session/topics.py` |
| DETECTION_WINDOW | TimeWindow | 写回 | actor、`routers/task.py` | `_slide_window` | `inference/online/topics.py` |
| LATEST_DETECTION | Latest | 写回 | viz | `_latest_detection` | `inference/online/topics.py` |
| LATEST_TEMPORAL | Latest | actor | viz | `_latest_temporal` | `inference/online/topics.py` |
| VIZ_LAST_TS | Latest | viz | viz | `_last_rendered_ts[task_id]` | `inference/online/topics.py` |
| RECONNECT_STATE | Latest | health_monitor | health_monitor | `_reconnecting_clients` | `daemons/health_monitor/topics.py` |

不进 topic 体系：告警账本（`_alarm_log` / `_alarm_seq` / `_alarm_gate`，实时告警为过渡功能，原样平移，见 A9）、启动埋点（`_startup_marks`，见 A4a）。

### 块 A：结构

| # | 决策 | 结论 | 否掉的选项与理由 |
|---|------|------|------------------|
| A1 | 每 run 对象装什么 | 新建 `Session`，只含身份、状态、topic 字典 | 保留 CQ 手写槽位：痛点全留；CQ 上 topic 化：CQ 仍兼管身份、告警、埋点 |
| A2 | 进程内还是外部中间件 | 进程内 topic | Redis Streams / ZeroMQ：帧约 0.9MB，回环实测 168–186 帧/s < 15 路需求 225 帧/s；无「只留最新值」语义 |
| A3 | 读写入口 | `sess.channel(T)` 返回实例后读写 | `T.push(sess, x)`：topic 兼管工厂与读写；`sess.push(T, x)`：每种语义每个操作一个转发，加语义要改 Session |
| A4 | 语义种类 | Latest / Fifo / TimeWindow 三种 | 自定义对象（Object）：无共性，不强行抽象；NumWindow：唯一使用方是告警，随 A9 砍掉 |
| A4a | 启动埋点 | 生产方自行判断首次，基建不提供首写钩子 | 首写钩子：为单一观测需求在基建埋点 |
| A4c | 时间语义 | TimeWindow 与 Fifo 按时间取走都读元素自带的 `ts`（数据时间），不读墙钟；元素须带 `ts`，在类型约束与接口注释里显式声明 | 墙钟：推理延迟或背压一抖，窗口覆盖的画面时长就变 |
| A5 | 实例挂在哪 | 挂在 `Session` 上 | 挂在 topic 上：close 须枚举全部 topic，漏一个即泄漏旧 run 的帧 |
| A6 | 实例何时创建 | 首次访问时创建；已 CLOSED 则现造不入表的空实例 | open 时全建：依赖 import 顺序；拥有方钩子注册：open 到注册之间读方缺槽 |
| A7 | topic 由谁声明 | 使用方模块声明，跨模块共享的放 `session/topics.py` | 集中在 session 包：推理侧改通道又要改 session 包（痛点 #1） |
| A8 | 基建边界 | 基建只承载存储语义、生命周期、通用可观测；不认识业务名词 | — |
| A9 | 告警账本 | 代码原样抽成 `AlarmLedger`，挂 `alarm_sink` 以 `Session` 为键的弱引用表；是「每 run 状态挂 Session」的唯一显式例外 | 装进 Latest：可变容器绕过写门，且首个告警落在 DRAINING 期会被拒 |

### 块 B：并发正确性

| # | 决策 | 结论 | 否掉的选项与理由 |
|---|------|------|------------------|
| B1 | 锁粒度 | 每 session 一把锁，护状态、topic 字典与全部 channel 数据 | 每 channel 一把：写门要两把锁配合论证；GIL 下拆细换不来并行（15 路模拟等锁 p99：一把 5.5µs / 每 channel 7.5µs） |
| B2 | 写门规则 | 统一「只有 ACTIVE 可写；读与清空任何状态放行」 | 各 topic 声明放宽：告警移出后无 DRAINING 期写入的 topic |
| B3 | 写门与状态切换 | 同一把 session 锁内 | 锁外检查：保留「清空后又追加一帧」的窗口 |
| B4 | 写方句柄 | `Session` 引用；channel 现取现用只写进规范，不做静态检查 | `RunIdentity`：每次读写回查注册表，core 反向依赖注册表 |
| B5 | 查表位置 | 只在入口（routers、health_monitor、run_control） | 写方按需查：可能「读旧写新」 |
| B6 | 跨「取走 + 交付」的原子性 | 业务方自持外层锁（现有唯一实例：recording 取帧锁）；session 锁不扩到交付 | 扩 session 锁：持锁调外部代码 |
| B7 | 锁序 | 三层锁序约定 + 锁表写进 DEVELOPMENT.md；批 1 盘点现有锁；不加运行期检查 | 运行期检查：嵌套对只有三四对，约定 + 评审可守住 |

B7 锁序约定：

```text
层级（高 → 低）   成员
L0 编排锁        run_control 任务锁（每 task 一把，唯一允许可重入）
L1 外层锁        业务方覆盖多步操作的锁：recording 取帧锁、推理 proxy 锁、渲染 proxy 锁
L2 叶子锁        session 锁、注册表写锁、告警账本锁、frame_ring 锁、各服务内部数据锁
```

1. 只能在持有高层锁时获取低层锁，反向禁止。
2. 同层不嵌套；L1 之间确需嵌套的，在约定里登记为固定有向对。
3. 持有叶子锁期间不获取任何锁，不调其他模块的代码（回调、上报、IO）。
4. 新增锁在定义处注释层级，并登记进锁表。

### 块 C：生命周期

| # | 决策 | 结论 | 否掉的选项与理由 |
|---|------|------|------------------|
| C1 | 每 run 辅助状态 | `Session` 上的 topic（`VIZ_LAST_TS`、`RECONNECT_STATE`），随 `close` 释放；服务内不建 task_id 表（例外仅 A9） | 服务自建表：脱离生命周期（痛点 #5） |
| C2 | 任务锁归属 | run_control | session_service：session 包背上编排职责 |
| C3 | 生命周期写入口 | 只经 run_control 持任务锁执行；fence 由 `expected=cq` 换为 `expected=sess` | — （批 0 已落地） |
| C4 | 状态机 | ACTIVE / DRAINING / CLOSED 三态 | 加 RECONNECTING：重连期写入规则不变，加状态无收益 |
| C5 | 同一 task 的注册约束 | 至多一个注册中的 `Session`；open 遇已有注册项即抛异常 | 新旧并存：迟到写与查表歧义 |
| C6 | 执行资源注册表 | decoder、actor 留在各自服务；增删只经 run_control | 并入 Session：执行资源与数据混在一起 |
| C7 | `stop_run` 顺序 | 维持 seal → 停 decoder → 停 workflow → 交出残帧 → close，三步各自 try | — （批 0 已落地） |

### 块 D：边界与迁移

| # | 决策 | 结论 | 否掉的选项与理由 |
|---|------|------|------------------|
| D1 | 对外与可观测 | 全部保持：admin `queue_depths` 的 5 个键、`[PRESSURE]` 的 component / resource、`frame_drop_total` 的 reason、`[startup]` 日志格式 | 借重构改名：影响前端、日志检索与看板；另做（见遗留更名事项） |
| D2 | 基建刻意不提供 | 订阅回调、阻塞等待新值、Fifo 遍历 / peek、运行期改容量、枚举某 run 的全部 topic | — |
| D3 | 迁移方式 | 桥接分批：CQ 挂 `cq.sess`，已迁调用点经它读写，批 7 删桥 | 一次切换：改动面过大，无法分批测绿 |
| D4 | 配置文件 | `config/client_config.yaml` 不改名，读取代码平移进 session 包 | 随重构改名：归配置整理提案 |

### 迁移批次

每批一份 update 记录，`pytest tests/` 全绿后进下一批。批 2–6 期间每批只迁完整的 topic，不出现同一份数据一半在 CQ、一半在 `Session`。

| 批 | 内容 | 验证重点 |
|----|------|----------|
| 0 | 已落地（[20261023_RUN_LIFECYCLE_FIXES](20261023_RUN_LIFECYCLE_FIXES.md)）；其中「重连表剔除」推迟到批 6 由 `RECONNECT_STATE` 解决 | — |
| 1 | session 包（T1）；锁序约定与盘点（T4） | 基建单测；锁盘点结论 |
| 2 | 桥接：start / stop 同步 open / seal / close；CQ 挂 `cq.sess` | 启停回滚；`stop_run` 及回滚后注册表无该 task |
| 3 | `LATEST_TEMPORAL` / `LATEST_DETECTION` / `VIZ_LAST_TS` | viz、写回 fence、算子框架 |
| 4 | `DETECTION_WINDOW` + `/task` 信号汇总迁出 | 算子框架、task 消息组装 |
| 5a | `READY` / `RAW` / `LATEST_RAW`；抽帧计数入 decoder；`first_frame` 埋点下放 decoder | 丢帧计数、压力日志；仓库外 15 路压测前后对比 |
| 5b | `DETECTIONS` / `PROCESSED` / `RENDERED` | 残帧交接顺序、录像 |
| 6 | 告警账本平移（A9）；`first_inference` 埋点下放写回；`RECONNECT_STATE` | 告警增量轮询与去重；重连；`/terminate` 后无残留 |
| 7 | 注册表切换；写方句柄换为 `Session`；任务锁移入 run_control；删桥 | 并发启停、身份 fence、全量 |
| 8 | 收尾删除（T6） | 全量；导入卫生 |

## 变更效果（预期）

| 维度 | 现状 | 改后 |
|------|------|------|
| 每 run 对象 | CQ：身份 + 状态 + 10 槽位 + 告警 + 埋点，36 个方法、6 把 payload 锁 | `Session`：身份 + 状态 + topic 字典，1 把锁 |
| 推理侧新增或改通道 | 改 client 包 | 改 `inference/online/topics.py` 与读写方 |
| 写门 | 逐方法手写、3 种变体、锁外检查 | channel 统一执行，与状态切换同锁 |
| 每 run 辅助状态 | viz / health_monitor 各建 task_id 表 | `Session` 上的 topic，随 `close` 释放（告警账本为显式例外） |
| 锁序 | 隐式，靠各处注释 | DEVELOPMENT.md 三层约定 + 锁表 |
| 拓扑查询 | 读 CQ + 各 worker | grep `channel(TOPIC)` 得全部读写点 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 残帧交接：`close` 先于交出残帧 | 残段真丢，无报警 | `test_run_control_shutdown`、`test_cq_drain_fence` 在批 5b / 7 持续锁住 |
| 同 session 热路径共用一把锁 | 15 路下主进程余量 | 批 5a 压测对比；真成瓶颈再单独给 READY 拆锁 |
| TimeWindow 淘汰前提「元素按 `ts` 非递减追加」 | 写回若乱序，旧帧滞留到下一轮才淘汰 | 批 4 核实写回是否每 run 有序，写进接口注释 |
| 锁盘点可能发现现有违规嵌套 | 潜在死锁 | 批 1 盘点结论单独上报，作为独立修复，不混进重构批次 |
| 桥接期两套结构并存 | 改动面大 | 桥只有 `cq.sess` 一个字段，批 7 删除；每批全量测绿 |
| KB 同步 | `SERVICE_CLIENT_STATE`、`ARCHITECTURE_DATA_FLOW`、`DESIGN_CONCURRENCY_AND_QUEUES`、`ARCHITECTURE_PACKAGE_LAYERS` 描述 client / CQ | 各批记录标「待沉淀」，由 kb-merge 融合 |

### 遗留更名事项（留给其他分支）

本重构为保持对外与可观测不变（D1、D4）而保留的 client / CQ 时代名字。改名会影响前端、日志检索或部署，各自另开分支处理：

| 名字 | 出现在 | 影响面 | 建议 |
|------|--------|--------|------|
| `[PRESSURE]` 的 component `client_queues` | 压力日志 | 日志检索、看板 | 改为 `session` |
| admin `clients[]` 与 `queue_depths` 的 `ca_ready` / `ca_raw` / `ca_processed` / `ca_detections` / `has_rendered` | `/admin` 响应（[docs/api/admin.md](../api/admin.md)） | 前端解析、API 文档 | 键名对齐 topic 名，与前端约定后改 |
| health 的 `total_clients` / `reconnecting_clients` / `orphan_streams` 等「客户端」字段 | `/health/status`、`/monitor/stats`（[docs/api/health.md](../api/health.md)） | 前端解析、API 文档 | 「有队列的客户端」改称 session，与前端约定后改 |
| `config/client_config.yaml` | 配置文件 | 部署、`.env` 与 deploy skill | 并入配置整理提案（`20260928_CONFIG_LAYOUT_PROPOSAL`） |
