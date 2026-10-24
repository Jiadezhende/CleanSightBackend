# 待推理帧 / 原始帧 / 最新原始帧迁成 topic，抽帧计数与首帧埋点下放 decoder

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 5a
> **知识库**：待沉淀

## 概述

CQ 的 `ca_ready`、`ca_raw`、`latest_raw_frame` / `latest_raw_timestamp` 迁成 `session/topics.py` 声明的 `READY`（Fifo 满拒新）、`RAW`（Fifo 满挤旧）、`LATEST_RAW`（Latest）。
抽帧计数器从 CQ 移进 decoder，`first_frame` 埋点由 decoder 自行判断首次。丢帧计数 reason、`[PRESSURE]` 的 component / resource、`[startup]` 日志格式都不变。

## 变更背景

- **承接**：批 4（[20261024_SESSION_MIGRATE_DETECTION_WINDOW](20261024_SESSION_MIGRATE_DETECTION_WINDOW.md)）后推理侧槽位已迁完，本批迁 decoder 产出的三份帧数据。
- 这三份数据跨 decoder / dispatcher / recording / viz / health_monitor 五个模块共享，按提案 A7 声明在 `session/topics.py`。

## 方案详情

### 全景

```text
decoder._process_frame
  ├─ RAW.push(frame)                 ──▶ recording：sweeper RAW.take(段长) / flush RAW.drain(until_ts)
  ├─ LATEST_RAW.set(frame)           ──▶ viz：叠框底图（拷一份）；health_monitor：最后一帧 ts
  │    写之前为空 → 本 run 首帧 → [startup] first_frame
  └─ 抽帧（每 N 帧留 1）→ READY.push ──▶ dispatcher：READY.pop
stream.get_pending_count = len(READY)；decoder 入口背压按 READY.maxlen 算占用率
```

| topic | 语义 | 丢帧计数 | 压力日志 | 替代 |
|-------|------|----------|----------|------|
| `READY` | `Fifo(ca_ready_maxlen, REJECT)` | 只计 `drops`（同旧 `frames_dropped_ready`，不计 Prometheus） | `client_queues` / `ca_ready`，水位 = 容量一半 | `ca_ready` + `append_ca_ready_with_throttle` / `pop_ca_ready` |
| `RAW` | `Fifo(ca_maxlen, EVICT)` | `raw_backpressure` | `client_queues` / `ca_raw`，水位 = max(容量一半, 1.5 段) | `ca_raw` + `append_ca_raw` / `take_raw_segment` / `drain_ca_raw` |
| `LATEST_RAW` | `Latest` | — | — | `latest_raw_frame` / `latest_raw_timestamp` / `get_latest_frame` |

| 改动 | 落在哪 |
|------|--------|
| topic 声明（容量取 client 配置，factory 内现取） | `app/services/session/topics.py` |
| `Frame.ts` 属性（= `timestamp`），满足按时间取走的元素约束 | `app/types/frame.py` |
| 写三个 topic、抽帧计数、首帧埋点 | `app/services/stream/decoder.py` |
| `get_pending_count` 读 `len(READY)` | `app/services/stream/service.py` |
| dispatcher 取帧、recording 取段 / 残帧、viz 取底图、health_monitor 取最后一帧 ts | 各自模块 |
| `[startup]` 日志抽成 `log_milestone`，CQ 剩下的两个里程碑也改用它 | `app/services/utils/startup_latency.py` |
| CQ 删上述字段与方法、`inference_decimation` / `ca_ready_maxlen` 入参与 `_raw_lock`；`cq_kwargs` 去掉这两项 | `client/queues.py`、`client/config.py` |

- **health_monitor「最后一帧 ts」**：`LATEST_RAW` 为空时取 `sess.info.started_at`，等价于旧 `latest_raw_timestamp` 的「初值 = CQ 创建时刻」。
- **抽帧计数随 decoder 走**：重连换新 decoder 后计数从 0 开始，抽帧相位可能偏一帧；保留率仍精确是 1/N。
- **首帧埋点**：旧实现是「decoder 收到第 1 帧」+ CQ 侧 first-wins 去重；新实现以「写之前 `LATEST_RAW` 为空」判定，decoder 是唯一写方，重连后的新 decoder 不会重报。
- **viz 底图拷贝**：保持旧 `get_latest_frame()` 的 `.copy()`，挪到 viz 侧做。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| READY 并发 | 无锁 SPSC（靠 GIL） | session 锁内 push / pop |
| CQ | 4 把 payload 锁 | 3 把 |

**自测结果**

| 项 | 结果 |
|----|------|
| decoder 写三个 topic、抽帧 1/N、首帧埋点每 run 一次、封闸后不收帧（新增 `test_decoder_session_writes`） | passed |
| 丢帧计数、压力日志沿用旧名与身份字段（`test_cq_pressure_log`）；栅栏 drain / 整段 take（`test_cq_drain_fence`） | passed |
| health_monitor 重连 / 放弃 / fence（`test_reconnect_on_initial_failure`，替身改为真 session 的 `LATEST_RAW`） | passed |
| 全量 `pytest tests/` | 1216 passed |
| 单机微基准（仓库外脚本，4×4 帧） | 单线程每帧 topic 读写 4.8µs（旧 0.24µs），15 路 × 30fps 合计约占 0.2% 单核；15 路 decoder + dispatcher + viz + sweeper 并发时，decoder 写路径 p50 11.6µs / p99 88µs |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 真机 15 路压测前后对比未做 | 微基准显示锁开销在 µs 级，但没覆盖真实 GIL 争用（解码读线程、渲染、推理收集线程） | 需人在 dev 环境跑仓库外 15 路压测，对比 `[PRESSURE]` / `[VIZ_THROUGHPUT]` 与 `frame_drop_total`；真成瓶颈再给 READY 单独拆锁 |
| topic 容量在 channel 创建时定死 | 改 `ca_*_seconds` 只对之后新开的 run 生效（与旧 CQ 一致） | 无 |
