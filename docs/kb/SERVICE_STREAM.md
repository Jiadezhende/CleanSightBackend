> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Stream Service

流服务为每个 run 管理一个 FFmpeg 解码器，把帧写入 ClientQueues。运行键 = int `task_id`（与注册表/decoder 字典一致），系统**只用 RTSP**。

## 主要职责与方法

`StreamService`（单例 `stream_service`）公开：

- `start_stream(task_id, stream_url)`：注册 decoder 并 `start()`（同步起，成功即返回）。
- `stop_stream(task_id)`：从 decoders 字典弹出并停。
- `restart_stream(task_id, stream_url) → bool`：**同步停旧** decoder → 建新 → 起新（reader 线程经 join 串行化，消除 `ca_ready` 双写窗口）。
- `get_stream_info(task_id) → {"url": ...} | None`、`get_pending_count(task_id) → int`（读 `ca_ready` 深度，供背压/健康）、`shutdown()`。

## FFmpegDecoder（自持读循环，RTSP-only）

- **自持线程**：`_reader_thread` 读 rawvideo stdout（合并双平台单一阻塞读路径，无外部 selector），`_stderr_thread` 读 stderr。
- **RTSP-only**：固定 RTSP 输入选项，已删 `protocol` 字段与 RTMP 分支。
- **输出规范化 CFR**：ffmpeg 用 `scale=W:H,fps=raw_fps` + `-vsync` 输出定尺寸定像素格式（默认 bgr24 / 640x480）的 CFR raw_fps rawvideo。
- **同步起、快速失败**：`start()` 在返回前同步抛 `FFmpegError` / `StreamConnectionError`（不延迟到线程）。
- **子进程回收**：`stop()` 内 `kill()` + `wait(timeout)` + 关管道；reader 线程必 join——即便快速失败的进程也回收，无僵尸。
- **帧时间戳**：`Frame.timestamp = time.time()`（读帧时墙钟到达时刻），非合成时钟。

写入去向：`ca_raw`（raw HLS 缓冲）、`ca_ready`（待推理）、`latest_raw_frame/latest_raw_timestamp`（健康监控/可视化）。

## 抽帧与背压

入 `ca_ready` 走 `ClientQueues.append_ca_ready_with_throttle()`：Bresenham 相位累加器按 `inference_fps/raw_fps` 均匀抽帧（支持非整除比，不依赖 wall-clock），队列满则丢（背压只丢推理帧，`ca_raw` 录制继续）。decoder 读 `manager.get_pending_count(task_id)` 判背压——此跨模块读是有意保留。

## URL 重写

`_rewrite_rtsp_url()` 仅当 URL 端口 == `settings.mediamtx_proxy_port` 时生效：host 固定 `127.0.0.1`、port 改 `settings.mediamtx_internal_port`、保留 userinfo——后端拉流绕过 RTSPProxy 直连本机 MediaMTX 内部端口。

## 健康监控输入

健康监控经 `latest_raw_timestamp` 判断断流，经 StreamService 查询/重启 decoder。重连接管在 GlobalHealthMonitor（见 [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)）：`start_stream` 失败后 decoder 仍留字典由监控异步重连，不再走 GuardedExecutor 双重重试。

## 代码来源

- `app/services/stream/service.py`
- `app/services/stream/decoder.py`
- `app/services/stream/config.py`
- `config/stream_config.yaml`
- `tests/test_stream_rewrite.py`
- `tests/test_reconnect_on_initial_failure.py`
