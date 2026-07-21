> 更新时间：2026-07-21
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Stream Service

流服务为每个 run 管理一个 FFmpeg 解码器，把帧写入 ClientQueues。运行键 = int `task_id`（与注册表/decoder 字典一致），系统**只用 RTSP**。

`StreamService` 已瘦成 **decoder 注册表 + 生命周期编排**：读帧由 decoder 自持线程完成，服务侧无 selector/轮询线程（构造 `StreamService()` 不再有起线程副作用）。跨模块只读 `client_manager`（顶层直接导入单例、boot 期 fail-fast，非惰性/吞异常）。

## 主要职责与方法

`StreamService`（单例 `stream_service`）公开：

- `start_stream(task_id, stream_url)`：注册 decoder 并 `start()`（同步起，成功即返回）。首次 `start()` 失败时 decoder **仍留字典**，由健康监控下个心跳重连（不抛出、不双重重试）。
- `stop_stream(task_id)`：从 decoders 字典弹出并**异步** `stop()`——terminal 路径，无新 run 复用该 CQ，迟到帧由 CQ 写门（DRAINING/CLOSED）拦截，异步安全、不阻塞 API。
- `restart_stream(task_id, stream_url) → bool`：**同步停旧** decoder（锁外 kill+reap+join，再入锁 cleanup+建新+`start()`）→ 建新 → 起新。旧 reader join 后新 reader 才写 `ca_ready`（无锁 SPSC deque），消除双生产者窗口；同时消除旧/新进程与 Phase-2 push 在 MediaMTX 同路径的连接竞争。捕获所有异常返回 `bool`，不阻塞健康监控线程。
- `get_stream_info(task_id) → {"url": ...} | None`（协议固定 RTSP、fps 取自 config，重连只需 url）、`get_all_task_ids() → set`（看**注册**不看 `is_alive()`，保留「死掉但仍注册」的 decoder 供重连；旧 `has_stream()` 已删——其存活判断会误清待重连 decoder）、`get_pending_count(task_id) → int`（读 `ca_ready` 深度，供背压/健康）、`shutdown()`（同步逐个 `stop()`，进程退出前清干净）。

## FFmpegDecoder（自持读循环，RTSP-only）

- **自持线程**：`_reader_loop`（`_reader_thread`）阻塞读 rawvideo stdout，双平台单一路径、无外部 selector；`_read_stderr_loop`（`_stderr_thread`）读 stderr。`start()` 末尾**无条件**起 reader 线程（原先仅 Windows 起）。循环起始处捕获 `stdout` 本地引用，避开 `stop()` 置 `self.proc=None` 的 TOCTOU；管道被关时 `read` 抛 `ValueError` 或返回 `b""` 均视为流结束、正常退出，**不自动重启**（重连交 `StreamHealthMonitor`）。
- **组合而非继承**：decoder 拥有一个 reader 线程，而非 `class FFmpegDecoder(threading.Thread)`——因 `start()` 需**同步**完成建流 + 秒退检测并抛 `FFmpegError` / `StreamConnectionError` 供 health monitor 首次感知；把 Popen 移进 `run()` 会丢掉这个同步失败信号。
- **RTSP-only**：固定 RTSP 输入选项内联为模块级 `_RTSP_INPUT_OPTS` 前缀，已删 `protocol` 字段与 RTMP 分支；ffmpeg 路径直接读 `settings.ffmpeg_path`（无 import 期快照）。
- **输出规范化 CFR**：ffmpeg 用 `scale=W:H,fps=raw_fps` + `-vsync drop` 输出定尺寸定像素格式（默认 bgr24 / 640x480）的 CFR raw_fps rawvideo。
- **同步起、快速失败**：`start()` 在返回前同步抛 `FFmpegError` / `StreamConnectionError`（不延迟到线程）；秒退分支 `raise` 前 `wait(timeout=1.0)` 回收僵尸，并按 stderr 标记区分 `StreamConnectionError`（可重试）与 `FFmpegError`（致命）。
- **子进程回收（无条件）**：`stop()` 无条件 `kill()`（直接 SIGKILL：仅解码到 pipe 无产物损坏、对端自有 gateway、卡死 socket 收不到 SIGTERM）+ `wait(timeout)`（`wait()` 移出 `if poll() is None`，对已退出进程立即 reap）+ 关管道；**锁外** join reader/stderr 两线程（对称回收，跳过自身线程）——即便快速失败的进程也回收，无僵尸。
- **帧时间戳**：`Frame.timestamp = time.time()`（读帧时墙钟到达时刻），非合成时钟。

写入去向：`ca_raw`（raw HLS 缓冲）、`ca_ready`（待推理）、`latest_raw_frame/latest_raw_timestamp`（健康监控/可视化）。

## 抽帧与背压

入 `ca_ready` 走 `ClientQueues.append_ca_ready_with_throttle()`：Bresenham 相位累加器按 `inference_fps/raw_fps` 均匀抽帧——每输入帧累加 `inference_fps`、跨过 `raw_fps` 阈值放行一帧，长期保留率精确 `= inference_fps/raw_fps`，支持非整除比（如 30→20 取 keep-keep-drop），**不依赖 wall-clock**。此设计取代了旧的「50ms 最小间隔门」：墙钟门从 30fps 源永远拿不到 20fps（算法天花板 ~15fps）、且受解码线程调度抖动把 15 漏成 ~12。默认 `inference_fps=15`（取 30 的整除值，`raw_fps=30`，见 [app/settings.py](../../app/settings.py)）。队列满则丢（背压只丢推理帧，`ca_raw` 录制继续）。`_decimate_phase` 仅由 decoder 线程读写、SPSC 无锁。decoder 读 `manager.get_pending_count(task_id)` 判背压——此跨模块读是有意保留。

> `inference_fps` 是 throttle、可视化 VizWorker target、persistence processed 段编码**共用的单一真源**（[app/settings.py](../../app/settings.py)）；改它会同步影响三处。

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
