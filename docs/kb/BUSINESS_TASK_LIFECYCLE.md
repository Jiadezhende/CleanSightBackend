> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 任务生命周期

一次 run 的起停由 `RunController`（控制面唯一编排出口）统一驱动，跨 stream / inference / persistence / client 各服务。运行键 = int `task_id`。编排细节见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## 启动流程

入口：`POST /api/start`（body 含 `task_id`、`rtsp_url` 等；`fps` 仅存 wire 不透传）。

1. API 层从 `clean_task` 查 `task_id`，校验存在、取 `source_ip`（被动身份字段）。
2. 经 `asyncio.to_thread` 桥接调 `run_controller.start_run(task_id, current_step, rtsp_url, source_ip)`（把同步持锁段挪出事件循环）。
3. `start_run` 全程持 `client_manager.lock_for(task_id)`（per-task RLock）：
   - 幂等/重启判断（见下）。
   - 建**新** CQ（`stage = resolve_stage(current_step)`，身份不可变）。
   - `persistence_manager.start_run(cq)` 清旧 HLS step 目录 + `inference_manager.start_workflow(cq)`（含 `FeatureStore.open_fresh` + 建 Actor）。
   - `stream_service.start_stream(task_id, rtsp_url)` 起解码。

## 幂等条件

同 task 已运行时，仅当 `step_id` 与流 URL **均未变**才幂等返回；任一变化（改 step / 换流）→ 先 `stop_run` 停旧、再全量重建（建新 CQ 换槽，不复用旧对象）。

## 终止流程

入口：`POST /api/terminate?task_id=...`（新，首选）或 `?client_id=<source_ip>`（旧，双模）。经 `to_thread` 调 `run_controller.stop_run(task_id, reason)`。健康监控的自动结束（重连失败/孤儿/超时）经 `cleanup_client` 同样委托 `stop_run`（并传 `expected` CQ 做对象身份 fence）。

`stop_run` 尽力而为、永不抛出，固定顺序：封闸 `to_draining()` → 停 decoder → 落 settlement 告警（`alarm_sink.persist_alarms`）+ flush HLS 残段 → 清 registry（`cq.close()`）→ 回收目录锁。

## 任务切换

同 task 再次 start 且 step/URL 变化即触发切换：先 `stop_run` 停旧、再建新 run。per-run 不可变 CQ 天然保证隔离——旧 run 的结算告警归属旧 CQ；晚到的旧 run 写入撞 DRAINING/CLOSED 状态门被拒，不串台到新 run（无需「先停旧 actor 再切字段」的排序不变式）。

## 代码来源

- `app/routers/api.py`
- `app/services/run_control.py`
- `app/services/inference/manager.py`
- `app/services/health_monitor/monitor.py`
- `app/services/client/manager.py`
- `tests/test_api_concurrency.py`
- `tests/test_teardown_identity_fence.py`
