> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Health Monitor Service

全局健康监控是自动化治理组件，负责断流重连、任务超时、孤儿状态清理和统一 cleanup。

## 启动位置

健康监控生命周期由 `app/routers/health.py` 管理，在 FastAPI lifespan 中早于 AI 服务进入。

## 检测对象

每轮检查读取：

- ClientManager 中所有 ClientQueues。
- StreamService 中所有 decoder client_id。
- 每个 ClientQueues 的 `latest_raw_timestamp`。

## 断流与重连

若有 decoder 但 `latest_raw_timestamp` 超过 `heartbeat_timeout` 未更新，进入重连模式。

重连状态记录：

- stream URL
- fps
- protocol
- attempt_count
- last_attempt_time
- last_frame_time_before_disconnect

到达 `reconnect_interval` 后调用 `stream_service.restart_stream()`。有新帧且足够新则退出重连模式。

## 清理条件

健康监控会触发清理的情况：

- 重连达到最大次数仍失败。
- 任务运行超过 `task_max_duration`。
- 有 ClientQueues 但无 decoder，超过 `orphan_timeout`。
- 有 decoder 但无 ClientQueues，立即停止孤儿 decoder。

## cleanup_client

`cleanup_client()` 是统一清理入口，API terminate、重连失败和孤儿流都使用它。

顺序：

1. 清理监控器内部状态。
2. 停止 decoder，除非 `skip_decoder=True`。
3. 调用 `InferenceManager.remove_client()` 触发结算告警和残余 HLS 落盘。
4. 调用 `ClientManager.remove_client()`。

函数不抛异常，返回每步结果和错误列表。

## 代码来源

- `app/services/health_monitor/monitor.py`
- `app/services/health_monitor/config.py`
- `app/services/health_monitor/types.py`
- `app/routers/health.py`
- `config/health_monitor_config.yaml`
- `tests/test_reconnect_on_initial_failure.py`

