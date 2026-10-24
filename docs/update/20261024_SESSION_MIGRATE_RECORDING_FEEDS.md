# 检测落盘缓冲 / 渲染帧队列 / 最新渲染帧迁成 topic

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 5b
> **知识库**：待沉淀

## 概述

CQ 的 `ca_detections`、`ca_processed`、`_latest_rendered` 迁成 `session/topics.py` 的 `DETECTIONS`、`PROCESSED`（Fifo 满挤旧）与 `RENDERED`（Latest）；`first_rendered` 埋点由 viz 自行判断首次。
至此 CQ 不再持有任何帧或检测数据，只剩身份、状态、告警账本与 `first_inference` 埋点。丢帧 reason、压力日志名、admin `queue_depths` 键不变。

## 变更背景

- **承接**：批 5a（[20261024_SESSION_MIGRATE_RAW_READY](20261024_SESSION_MIGRATE_RAW_READY.md)）迁完 decoder 产出的三份帧数据。本批迁 recording 消费的另外两条与 WS 读的渲染帧。

## 方案详情

### 全景

```text
写回 ── DETECTIONS.push ─────────────┐
viz ─── PROCESSED.push ──────────────┼──▶ recording：sweeper take(段长) / DETECTIONS.drain()
    └── RENDERED.set（写前为空 → [startup] first_rendered）──▶ WS /ai/video 读
stop_run：seal → … → 清前端槽 RENDERED.clear() → flush_residual（PROCESSED / RAW drain(None)、DETECTIONS drain）→ close
```

| topic | 语义 | 丢帧计数 | 压力日志 | 替代 |
|-------|------|----------|----------|------|
| `DETECTIONS` | `Fifo(ca_maxlen, EVICT)` | `detection_backpressure` | 无（同旧） | `ca_detections` + `append_ca_detections` / `drain_ca_detections` |
| `PROCESSED` | `Fifo(ca_maxlen, EVICT)` | `hls_backpressure` | `client_queues` / `ca_processed`，水位同 RAW | `ca_processed` + `append_ca_processed` / `take_processed_segment` / `drain_ca_processed` |
| `RENDERED` | `Latest` | — | — | `set_latest_rendered` / `get_latest_rendered` |

| 改动 | 落在哪 |
|------|--------|
| topic 声明；RAW / PROCESSED 共用 `_segment_fifo` 工厂 | `app/services/session/topics.py` |
| 写回 push DETECTIONS；viz push PROCESSED、set RENDERED、首帧埋点 | `detection/service.py`、`visualization_worker.py` |
| recording 取 PROCESSED / DETECTIONS；run_control 清 RENDERED；WS 读 RENDERED | `recording/service.py`、`run_control/service.py`、`routers/ai.py` |
| CQ 删上述字段、方法与 `_viz_lock` / `_slide_window_lock` / processed 压力 reporter；`get_queue_depths` 改读 topic | `app/services/client/queues.py` |

- **残帧交接顺序不变**：`flush_residual` 在 `stop_run` 里早于 `client_service.remove`（内含 `cq.close` → `sess.close`）与 `session_service.close`；flush 时 session 已 DRAINING，读与取走照常放行。
- **`RENDERED` 写门**：旧 `set_latest_rendered(None)` 在非 ACTIVE 时放行；现在拆除期清空走 `clear()`（任何状态放行），非空写只在 ACTIVE 放行，效果相同。

## 变更效果

| 维度 | 变更前（本批前） | 变更后 |
|------|------------------|--------|
| CQ 数据 | processed / detections / 渲染帧 + 告警 | 只剩告警账本 |
| CQ 锁 | 3 把 payload 锁 | 1 把（告警） |

**自测结果**

| 项 | 结果 |
|----|------|
| 残帧交接：flush 时 RAW / PROCESSED / DETECTIONS 仍可读、session 为 DRAINING，拆完 CLOSED 且释放（新增 `test_session_bridge::test_residual_frames_still_readable_at_flush_then_released`） | passed |
| 录像编排（`test_recording_service`，替身改为按 topic 取帧）、栅栏 / 整段（`test_cq_drain_fence`）、压力日志（`test_cq_pressure_log`）、落盘缓冲（`test_client_detections_buffer`） | passed |
| viz 写回与 `first_rendered` 每 run 一次（`test_viz_worker_render_proxy`） | passed |
| 全量 `pytest tests/` | 1221 passed |

## 遗留风险 / 后续任务

无（批 6 迁告警账本、`first_inference` 埋点与重连状态）。
