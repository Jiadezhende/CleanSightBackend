# session 基建落地 + 锁序约定与现有锁盘点

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 1；新包尚无调用方
> **知识库**：待沉淀

## 概述

新建 `app/services/session/`：`Session`（身份 + 状态机 + topic 字典）、`Topic` 声明、Latest / Fifo / TimeWindow 三种存储语义、`SessionService` 注册表与单例。
`docs/DEVELOPMENT.md` 新增 §9 锁序（三层约定 + 锁表），并盘点 `app/` 现有全部锁。运行时行为不变：新包此批无调用方。

## 变更背景

- **承接**：[20261022_SESSION_MQ_PROPOSAL](20261022_SESSION_MQ_PROPOSAL.md) 的批 1（T1 建 session 包、T4 锁序约定与盘点）。提案要用按 run 隔离的 topic 替代 `ClientQueues`（CQ）的手写槽位；本批只落基建，调用点从批 2 起分批迁移。
- **痛点**：CQ 每个写方法各自手写写门（3 种变体、锁外检查）；释放时按固定顺序同持 6 把锁；锁序全靠各处注释。

## 方案详情

### 全景

```text
Topic("X", factory)                    使用方模块声明；factory 零参，带好业务参数（容量 / 丢帧 reason / 压力名）
  └─ sess.channel(X)                   首次访问 → 锁外调 factory → 绑定 session → 入表（并发首访只留一个）
       ├─ 写 set / push               session 锁内：state 是 ACTIVE 才写
       └─ 读 get / pop / take / drain / snapshot / clear   任何状态放行
SessionService.open(info) → seal(sess) → close(sess)   close 摘表（身份核对）并释放全部 channel 数据
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| `Session` / `SessionInfo` / `SessionState` / `Topic` | `session/core/session.py` | §1 |
| `Latest` / `Fifo` / `TimeWindow` / `PressureSpec` | `session/core/channels.py` | §2 |
| 注册表 + 单例 | `session/service.py`、`session/instance.py` | §3 |
| 锁序约定、锁表 | `docs/DEVELOPMENT.md` §9 | §4 |

### 1. `Session`

- 一把 `threading.Lock`（L2），护 `_state`、topic 字典与全部 channel 数据。channel 绑定时借用这把锁，写门检查与写入同锁。
- `seal()`：ACTIVE → DRAINING；`close()`：→ CLOSED，摘掉全部 channel 并在锁内清空数据。之前被捕获的 channel 引用也读到空，写被拒。
- 压力 reporter 的 reset 在锁外做（规则 3）。
- `channel(T)` 在 CLOSED 后返回现造的、不入表的空实例（提案 A6）。
- `SessionInfo.started_at` 是 epoch 秒，取代 CQ 的 `task_started_at`。

### 2. 三种语义

| 语义 | 写 | 读 / 取走 | 可观测 |
|------|----|-----------|--------|
| `Latest(default)` | `set` | `get`；`clear` 回到 default | — |
| `Fifo(maxlen, on_full=REJECT/EVICT, drop_reason, pressure)` | `push -> bool` | `pop` / `take(n)` / `drain(until_ts)` / `len` | `drops`；`drop_reason` 给了就计 `frame_drop_total`；`PressureSpec` 给了就喂 `[PRESSURE]`（identity 取自 session） |
| `TimeWindow(span)` | `push`（淘汰 `ts < 新元素.ts - span`） | `snapshot` / `len` | — |

- 按时间的操作只读元素的 `ts`（数据时间）：`drain(until_ts)`、TimeWindow 淘汰、压力行的队头年龄。元素约束为 `Timestamped` 协议。
- 丢帧计数与压力上报一律在 session 锁外。
- **与提案 D2 的一处偏差**：`TimeWindow.extend_span(s)` 只增不减。帧窗保留时长 = max(10s, 该 run 各算子最大感受野)，要等 `start_workflow` 实例化算子后才知道；而 channel 可能更早被 `/task` 读方创建。只增的放宽保持现有 `set_stream_windows` 语义，不提供缩小与 Fifo 改容量。

### 3. 注册表

- COW 不可变 dict，读免锁；`_wlock`（L2）只串行写。
- `open` 遇同 task 已注册即抛 `SessionConflictError`（提案 C5）。
- `close(sess)` 只在注册项仍是 `sess` 时摘表，`sess` 本身一律 CLOSED，所以迟到的旧 close 不会摘掉新 run。

### 4. 锁序约定与盘点结论

约定原文见 [DEVELOPMENT.md §9](../DEVELOPMENT.md)。盘点了 `app/` 与 `mediamtx_gateway/` 的全部锁：

- **无死锁环**：所有嵌套都单向，被嵌套方从不回调持锁方。`mediamtx_gateway/` 没有线程锁。
- **两个 proxy 的 `_lock` 只能归 L1**：`_lock` 内要取 frame_ring 锁（归还槽位与 reset 必须同锁互斥，见 `infer_proxy.py` 注释）。
- **锁内做 IO 但不嵌套应用锁的归 L1**：decoder `lock`、离线作业 `_lock`、两个 `_proc_lock`、lab `_LOCK`、各懒加载锁。

现有违规如下，按提案作为独立修复，**不混进本重构**：

| 编号 | 位置 | 违反 | 影响 | 处理 |
|------|------|------|------|------|
| V1 | `stream/service.py` `_start_stream_impl` / `restart_stream`：持 `self.lock`（L2）调 `dec.start()`（取 decoder `lock` + Popen + `sleep(0.1)`） | 规则 1、3 | 每起一路流，全部 task 的 `get_stream_info` / `is_decoder_alive` / `stop_stream` 被挡 ≥100ms；Popen 卡住即全局停顿。不死锁 | 待人决策：锁内只注册、锁外 `start()`（任务锁已保证同 task 串行） |
| V2 | 同文件持 `self.lock` 调 `dec.is_alive()`（取 decoder `lock`，`stop()` 最多持 2s） | 规则 1 | 现在无害（并发观察者同线程或同持任务锁），但脆弱 | 随 V1 一起改：锁内取引用、锁外判活 |
| V3 | CQ `_raw_lock` / `_viz_lock` / `_slide_window_lock` 内调 Prometheus `inc` | 规则 3 | 良性（Prometheus 锁是终端锁） | 随 CQ 删除消失（批 8）；session channel 已在锁外计数 |
| V4 | CQ `_state_lock` 内 reset 3 个 PressureReporter | 规则 2 | 良性 | 同上 |
| V5 | CQ `_release_payload` 同持 6 把 L2 | 规则 2 | 良性（无其他路径同持其中两把） | 同上；`Session.close` 只持一把 |

另记（不违规）：recording `_collect_lock` 是跨 task 的 L1，持锁期间 `put(timeout=1.0)`，HLS 队列满时 task A 的 `stop_run` 可能被 sweeper 处理 task B 拖慢「段数 × 1s」；20261023 记录已列为观察项。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 写门 | CQ 逐方法手写、锁外检查 | channel 统一执行，与状态切换同锁 |
| 释放 | 同持 6 把锁 | 一把 session 锁内清空全部 channel |
| 锁序 | 隐式 | DEVELOPMENT.md §9 三层约定 + 锁表 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_session_core.py`、`tests/test_session_service.py` | 23 passed |
| 导入门禁（新增 `app.services.session*` 三条 BUDGET） | passed |
| 全量 `pytest tests/` | 1205 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| V1 / V2 | 起流期间全局读 stream 注册表被挡 | 独立修复，待人决策 |
| 同 session 热路径共用一把锁（READY 从无锁 SPSC 改为持锁） | 15 路下主进程余量 | 批 5a 压测对比 |
