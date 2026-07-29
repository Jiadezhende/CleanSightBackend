> 更新时间：2026-07-21
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

## 断流与重连（判据 = decoder 进程死活）

判据是**后端 decoder 子进程是否存活**（`stream_service.is_decoder_alive(task_id)`），**不是**帧 staleness。依据：实测 RTSP 断流时后端 ffmpeg 从 TCP 控制通道即收 EOF 退出（decoder 的 `-timeout` 兜底把「真·网络分区」下的挂死也转成退出），故进程死活能干净区分——

- **进程已退出**（断流 EOF / 崩溃 / 首启失败）→ 进入重连模式，按 `reconnect_interval` 节流反复 `restart_stream()`（respawn）；某次 respawn 起活进程并来足够新的新帧 → 退出重连（成功）。比旧的 5s staleness 判据更快感知。
- **进程活着但暂无帧**（等首个关键帧 / 瞬时停）→ **只等，不杀**（根治「等首帧被误杀→重连→再等一个 GOP」的启动延迟翻倍 bug）。

`ReconnectState`（`types.py`）字段：`task_id`、`stream_url`、`last_attempt_time`（respawn 节流）、`last_frame_time_before_disconnect`（判新帧）、`cq`（身份 fence）；`attempt_count` 已停用（不再数次数，保留字段作兼容）。无 fps/protocol 字段（固定 RTSP、fps 走配置）。

## 清理条件

触发清理的情况：

- 重连中**无帧时长 ≥ cleanup_timeout**（配置项，默认 20s；纯时间触发，重连本身不数次数——`max_reconnect_attempts` 连同「heartbeat + interval×attempts」的派生式已一并删除）。
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

