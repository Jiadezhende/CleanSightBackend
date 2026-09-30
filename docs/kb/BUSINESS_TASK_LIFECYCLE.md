> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 任务生命周期

一次 run 的起停由 `RunControlService`（控制面唯一编排出口）统一驱动，跨 stream / inference / recording / alarm / client 各服务。运行键 = int `task_id`。编排细节见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## run 与 step 的关系

- 同一 `(task_id, step_id)` 每开跑一次就是一个新 run，盘上一个目录 `{task}/{step}/{run_id}/`；`run_id` = 开跑时刻（epoch 毫秒），同 step 内严格递增。身份 `RunIdentity(task_id, step_id, run_id)`（`app/types/run.py`）。
- 一个 CQ == 一次 run：CQ 构造时带上 `RunIdentity`，此后不变。
- 回放、追溯、送标都以 run 为单位：缺省取该 step 最新可见 run，可带 `run_id` 点名旧 run。timeline 告警按 run 存续期 `[run_id, 下一个 run_id)` 归属。
- 离线分析锁定一个 run：对正在运行的 run 提交返回 409（输入还在写），可对旧 run 点名跑。

## 启动流程

入口：`POST /api/start`（body `{ task_id, rtsp_url }`；历史字段 `fps` 已弃用——后端从不使用，老前端继续带无害，新前端可省）。

1. API 层经 `db_tasks.query_task(task_id)`（`app/db/tasks.py`）查 `clean_task`，校验存在、取 `source_ip`（被动身份字段）与 `current_step`；查完即还连接，起流期间不持有 DB 连接。
2. 经 `asyncio.to_thread` 桥接调 `run_control_service.start_run(task_id, current_step, rtsp_url, source_ip)`（把同步持锁段挪出事件循环）。
3. **参数校验（锁外、动旧 run 之前）**：`current_step` 非数字 / 推理配置未定义该 step / 该 step 无在线检测 → 400，已在跑的旧 run 不受影响。
4. 其余全程持 `client_service.lock_for(task_id)`（per-task RLock）：
   - 幂等/重启判断（见下）。
   - **分配本次 run**：`runs.allocate` 建 `{task}/{step}/{run_id}/`，得到 `RunIdentity`。
   - 建**新** CQ（带 `run` 与已解析的 `stage`，身份不可变）后 `client_service.set` 注册（set/remove 均归 RunControlService，与 `stop_run` 对称）。
   - `inference_service.start_workflow(cq)`：只建 Actor，不碰存储。
   - `stream_service.start_stream(task_id, rtsp_url)` 起解码。
   - 注册后的 setup 步全包进 `try`：任一步失败 → `stop_run(expected=cq)` 对称回滚注销、重抛，不留泄漏 CQ。

**换代不清任何旧产物**：新 run 写新目录；旧 run 的录像与检测结果保留到 step TTL，列表 / 回放可带 `run_id` 点名。

## 幂等条件

同 task 已运行时，仅当 `step_id` 与流 URL **均未变**才幂等返回；任一变化（改 step / 换流）→ 先 `stop_run` 停旧、再全量重建（分配新 run、建新 CQ 换槽，不复用旧对象）。

## 终止流程

入口：`POST /api/terminate`，双模——body `{ task_id }`（新，首选，与 start 对称）或 query `?client_id=<source_ip>`（旧，兼容期保留）。经 `to_thread` 调 `run_control_service.stop_run(task_id, reason)`。健康监控的自动结束（重连无帧超时 / 孤儿 / 任务超时）经 `cleanup_client` 同样委托 `stop_run`（并传 `expected` CQ 做对象身份 fence）。

`stop_run` 尽力而为、永不抛出，固定顺序：封闸 `to_draining()` → 停 decoder → 落 settlement 告警（`alarm_sink.persist_alarms`）+ 交出 HLS 残段与剩余检测结果（`recording_service.flush_residual(cq)`，须在 `cq.close()` 之前）→ 清 registry（`cq.close()`）。

## 任务切换

同 task 再次 start 且 step/URL 变化即触发切换：先 `stop_run` 停旧、再建新 run。per-run 不可变 CQ 天然保证内存侧隔离——旧 run 的结算告警归属旧 CQ；晚到的旧 run 写入撞 DRAINING/CLOSED 状态门被拒，不串台到新 run（无需「先停旧 actor 再切字段」的排序不变式）。盘上隔离靠一 run 一目录：旧 run 迟到的落盘写进它自己的目录。同 step 重启后，缺省回放指向最新可见 run；新 run 首段 HLS 写出前（~10s）不带 `run_id` 的回放为空，不回落上一次录像。

## 代码来源

- `app/routers/api.py`、`app/db/tasks.py`（`query_task`）
- `app/services/run_control/service.py`
- `app/storage/runs.py`（`allocate`）、`app/types/run.py`
- `app/services/inference/online/service.py`（`resolve_stage` / `start_workflow`）
- `app/services/recording/service.py`（`flush_residual`）
- `app/daemons/health_monitor/worker.py`（`cleanup_client`）
- `app/services/client/service.py`
- `app/services/inference/offline/service.py`（运行中 run 提交 409）
- `tests/test_api_concurrency.py`、`tests/test_start_rollback.py`、`tests/test_teardown_identity_fence.py`
