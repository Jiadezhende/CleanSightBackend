> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Health Monitor Service

全局健康监控是自动化治理组件，负责断流重连、任务超时、孤儿状态清理和统一 cleanup。全程按 int `task_id` 键。

## 启动位置

健康监控生命周期由 `app/routers/health.py` 管理，在 FastAPI lifespan 中早于 AI 服务进入。

## 检测对象

每轮检查读取：

- `client_manager.snapshot()`（`{task_id → ClientQueues}`）。
- StreamService 中所有 decoder 的 task_id。
- 每个 ClientQueues 的 `latest_raw_timestamp`。

内部状态按 task_id 键：`_reconnecting_clients: {task_id → ReconnectState}`、`_last_activity: {task_id → float}`。

## 断流与重连

若有 decoder 但 `latest_raw_timestamp` 超过 `suspect_timeout`（= heartbeat_timeout，默认 5s）未更新，进入重连模式。`ReconnectState`（`types.py`）字段：`task_id`、`stream_url`、`attempt_count`、`last_attempt_time`、`last_frame_time_before_disconnect`、`cq`（捕获的 CQ 对象引用，作身份 fence 依据）。**已删除 fps/protocol 字段**（固定 RTSP，fps 走配置）。到达重连间隔后调 `stream_service.restart_stream()`；有足够新的新帧则退出重连。

## 清理条件

触发清理的情况：

- 重连达最大次数仍失败。
- 任务运行超过 `task_max_duration`（默认 7200s，0=禁用）。
- 有 ClientQueues 但无 decoder，超过 `orphan_timeout`（默认 30s）。
- 有 decoder 但无 ClientQueues，立即停止孤儿 decoder。

## cleanup_client → 委托 RunController

`cleanup_client(task_id, reason, skip_decoder=False, expected=None)` 是统一清理入口（API terminate 走 api→RunController，重连失败/孤儿/超时走本入口），**直接委托** `run_controller.stop_run(task_id, reason, skip_decoder=skip_decoder, expected=expected)`——不再各自调 `InferenceManager.remove_client` / `ClientManager.remove_client`。

`expected` 传监控线程在**决策时刻**捕获的 CQ 对象引用：monitor 线程「先决策后拿锁」，决策→拿锁间槽位可能被 `/start` 重启换新 CQ，`stop_run` 内以 `expected` 做对象身份 fence，槽位已非 expected 则整段放弃，防误删健康新 run。拆机顺序（封闸→停 decoder→落 settlement/HLS→清 registry）由 RunController 统一，见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## 代码来源

- `app/services/health_monitor/monitor.py`
- `app/services/health_monitor/config.py`
- `app/services/health_monitor/types.py`
- `app/services/run_control.py`（`cleanup_client` 委托的 `stop_run`）
- `app/routers/health.py`
- `config/health_monitor_config.yaml`
- `tests/test_reconnect_on_initial_failure.py`
- `tests/test_teardown_identity_fence.py`

