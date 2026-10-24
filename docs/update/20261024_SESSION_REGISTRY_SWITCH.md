# 注册表切到 session_service、写方句柄换成 `Session`、任务锁移入 run_control，删迁移桥

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 7
> **知识库**：待沉淀

## 概述

运行态注册表从 `client_service`（CQ）切到 `session_service`（`Session`）：run_control 只注册 session、不再建 CQ；
所有写方与读方改为直接持 `Session`（推理请求、写回句柄、actor、viz、decoder、recording、health_monitor、routers）；
per-task 生命周期锁从 `ClientService` 移入 `RunControlService`。CQ 与 `client_service` 此后无调用方，留待批 8 删除。对外接口、日志与指标名不变。

## 变更背景

- **承接**：批 6（[20261024_SESSION_ALARM_LEDGER_RECONNECT](20261024_SESSION_ALARM_LEDGER_RECONNECT.md)）后 CQ 只剩身份、状态与 `cq.sess` 一个桥字段，所有数据都在 session 上。
- 桥接期每个写方都绕 `cq.sess` 一跳，状态在 CQ 与 session 上各存一份（CQ 镜像到 session）；提案 C2 要求任务锁归编排方 run_control。

## 方案详情

### 全景

```text
/start ── run_control.start_run（持 run_control.lock_for(task_id)）
            session_service.open(SessionInfo) → inference.start_workflow(sess) → stream.start_stream
/terminate、health_monitor ── run_control.stop_run(task_id, expected=sess)
            fence 核对 → seal → 停 decoder → stop_workflow(sess) + 结算 → 清前端槽 → recording.flush_residual(sess)
            → session_service.close(sess)
入口查表（routers、health_monitor、run_control、各 worker 的遍历）：session_service.get / snapshot / find_by_source_ip
写方句柄：DetectionTask.sess → _Pending.sess → FrameDetection.sess → 写回；render ctx (task_id, sess, ts)；
          decoder.sess；actor._sess；recording 每次调用传 sess
```

| 部件 | 旧 | 新 | 落在哪 |
|------|----|----|--------|
| 注册表 | `client_service`（CQ） | `session_service`（Session） | 全部入口 |
| 任务锁 | `client_service.lock_for` | `run_control_service.lock_for`（L0，RLock） | `run_control/service.py` |
| fence | `expected=cq` | `expected=sess` | run_control、health_monitor |
| 写回句柄 | `FrameDetection.cq` / `DetectionTask.cq` / `_Pending.cq` | `.sess` | `app/types/detection.py`、`inference/online/types.py`、`infer_proxy.py` |
| actor / workflow | `start_workflow(cq)` / `ClientTemporalActor(cq=)` | `start_workflow(sess)` / `ClientTemporalActor(sess=)` | `inference/online/` |
| decoder | `FFmpegDecoder(client_queues=)` | `FFmpegDecoder(sess=)` | `stream/` |
| recording | `collect_from(cq)` / `flush_residual(cq)`，段长取 `cq.ca_segment_len` | `collect_from(sess)` / `flush_residual(sess)`，段长现取 client 配置 | `recording/` |
| 注入名 | `clients=` / `client_service_instance=` / `client_service=` | `sessions=` / `session_service=` | recording、dispatcher、DetectionService、离线作业、health_monitor |
| admin 深度 | `cq.get_queue_depths()` | `session/topics.queue_depths(sess)`（5 个键名不变） | `routers/admin.py`、`inference/online/service.py` |

- **状态只有一份**：CQ 镜像删除后，seal / close 只经 `session_service`，由 run_control 持任务锁调用。
- **start 回滚**：注册 session 后任一步失败，`stop_run(expected=sess)` 回滚；session 不泄漏在注册表。
- **离线作业 409**：「该 run 正在跑」的判据改为 `session_service.get(task_id).info.run == run`。
- 测试侧：`factories.make_cq` / `make_bare_cq` 换成 `make_session`（不入表）与 `open_session`（入全局注册表，conftest 每个用例换空表）；`test_cq_state_machine` 更名 `test_session_state_machine`，`test_session_bridge` 更名 `test_run_control_session`；`ClientService.find_by_source_ip` 的用例删除（`test_session_service` 已覆盖同一语义）。

## 变更效果

| 维度 | 变更前（桥接期） | 变更后 |
|------|------------------|--------|
| 写方句柄 | CQ，经 `cq.sess` 读写 | `Session` 直接读写 |
| run 状态 | CQ 与 session 各一份（镜像） | 只在 session |
| 任务锁 | client 包 | run_control（编排方） |
| client 包 | 注册表 + CQ + 配置 | 只剩配置有调用方（批 8 平移） |

**自测结果**

| 项 | 结果 |
|----|------|
| 并发启停：同 task 幂等 / 改 URL 重启 / start 与 terminate 串行 / 不同 task 并行 / terminate 持锁（`test_api_concurrency`） | passed |
| 身份 fence：stop_run / restart_stream / stop_orphan_decoder 命中与未命中（`test_teardown_identity_fence`）；start 回滚（`test_start_rollback`）；停机逐个拆（`test_run_control_shutdown`）；任务锁可重入、残帧交接（`test_run_control_session`） | passed |
| 写回 fence、推理代理、dispatcher、viz、decoder、recording、health_monitor、routers 相关用例 | passed |
| `import app.main` | 通过 |
| 全量 `pytest tests/` | 1221 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `app/services/client/` 中 `ClientService` / `ClientQueues` 已无调用方，配置读取仍在 client 包 | 死代码 | 批 8：删 client 包、配置读取平移进 session 包、导入门禁、DEVELOPMENT.md §3 |
| dev 环境真流端到端未跑 | 改动覆盖启停 / 重连 / 录像全链路，单测替身覆盖不到真实 ffmpeg / 推理子进程 | 需人在 dev 环境跑一次断流重连、拆除、录像回放 |
