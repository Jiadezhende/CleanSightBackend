> 更新时间：2026-09-20
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
- `get_stream_info(task_id) → {"url": ...} | None`（协议固定 RTSP、fps 取自 config，重连只需 url）、`get_all_task_ids() → set`（看**注册**不看 `is_alive()`，保留「死掉但仍注册」的 decoder 供重连——若按 `is_alive()` 判断会误清待重连 decoder）、`get_pending_count(task_id) → int`（读 `ca_ready` 深度，供背压/健康）、`shutdown()`（同步逐个 `stop()`，进程退出前清干净）。

## FFmpegDecoder（自持读循环，RTSP-only）

- **自持线程**：`_reader_loop`（`_reader_thread`）阻塞读 rawvideo stdout，双平台单一路径、无外部 selector；`_read_stderr_loop`（`_stderr_thread`）读 stderr。`start()` 末尾**无条件**起 reader 线程（原先仅 Windows 起）。循环起始处捕获 `stdout` 本地引用，避开 `stop()` 置 `self.proc=None` 的 TOCTOU；管道被关时 `read` 抛 `ValueError` 或返回 `b""` 均视为流结束、正常退出，**不自动重启**（重连交 `StreamHealthMonitor`）。
- **组合而非继承**：decoder 拥有一个 reader 线程，而非 `class FFmpegDecoder(threading.Thread)`——因 `start()` 需**同步**完成建流 + 秒退检测并抛 `FFmpegError` / `StreamConnectionError` 供 health monitor 首次感知；把 Popen 移进 `run()` 会丢掉这个同步失败信号。
- **RTSP-only**：固定 RTSP 输入选项由模块级函数 `_rtsp_input_opts()` 出，作 `-i` 之前的前缀，无 `protocol` 字段/RTMP 分支；ffmpeg 路径直接读 `settings.ffmpeg_path`（无 import 期快照）。选项**按调用取值、不在 import 期定死**——定死则改 env 要重启进程才生效。
- **输出规范化 CFR**：ffmpeg 用 `scale=W:H,fps=raw_fps` + `-vsync drop` 输出定尺寸定像素格式（默认 bgr24 / 640x480）的 CFR raw_fps rawvideo。
- **同步起、快速失败**：`start()` 在返回前同步抛 `FFmpegError` / `StreamConnectionError`（不延迟到线程）；秒退分支 `raise` 前 `wait(timeout=1.0)` 回收僵尸，并按 stderr 标记区分 `StreamConnectionError`（可重试）与 `FFmpegError`（致命）。
- **子进程回收（无条件）**：`stop()` 无条件 `kill()`（直接 SIGKILL：仅解码到 pipe 无产物损坏、对端自有 gateway、卡死 socket 收不到 SIGTERM）+ `wait(timeout)`（`wait()` 移出 `if poll() is None`，对已退出进程立即 reap）+ 关管道；**锁外** join reader/stderr 两线程（对称回收，跳过自身线程）——即便快速失败的进程也回收，无僵尸。
- **帧时间戳**：`Frame.timestamp = time.time()`（读帧时墙钟到达时刻），非合成时钟。

写入去向：`ca_raw`（raw HLS 缓冲）、`ca_ready`（待推理）、`latest_raw_frame/latest_raw_timestamp`（健康监控/可视化）。

## 拉流读超时：判死延迟是 `-timeout` 的 2 倍

`_rtsp_input_opts()` 里的 `-timeout T`（微秒）是**socket 读超时**，取自 `settings.rtsp_read_timeout_s`（默认 2.5s，env `CLEANSIGHT_RTSP_READ_TIMEOUT_S`）。三条硬约束：

- **`-timeout T` 的实际判死延迟是 `2T`，不是 `T`**。ffmpeg 第一次读超时**不致命**——它重试一次，第二次超时才退出。故该 flag 的真实语义是「连续 `2T` 收不到任何字节才判死」。这是 ffmpeg demux 层自带的重试，**配置改不掉**：逐个去掉 `-err_detect ignore_err` / `-fflags nobuffer+discardcorrupt`、乃至只剩 `-rtsp_transport tcp -timeout` 的最小组合，行为都是 2×。实测（ffmpeg n7.1.4，真·网络分区场景——中继停止双向转发但不 close、不发 FIN）：

```text
  -timeout    首次触发               退出                    比值
    2.0s      —                      4.50s                   2.25×
    2.5s      2.75s                  5.41s                   2.16×
    5.0s      5.11 / 5.12 / 5.26s    10.51 / 10.56 / 10.42s  2.10×
   10.0s      —                      20.28s                  2.03×
```

  比值随 T 增大收敛到 2 ⇒ 是「两次等待 + 常数开销」，没有第三次。

- **第一次超时之后仍可恢复**：冻结跨过第一次超时、在第二次之前解冻，关键帧一来立刻恢复解码，进程不退、不算断流。
- **`2T` 必须明显小于 `cleanup_timeout`**（health_monitor，默认 20s）：两个值分居两处配置却是串联的——`cleanup_timeout` 从最后一帧算起，静默断流下 decoder 先花 `2T` 才退出，剩下的才是 respawn + 建连 + 等首个关键帧的预算。`2T ≥ cleanup_timeout` 时进程还没死就先被判 cleanup 拆除，重连永不触发。`GlobalHealthMonitor.start()` 有一条越界告警（只告警不纠正，见 [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)）。
- **`-rtsp_transport tcp` 不可换 udp**：UDP 下「会话建成却 0 RTP」在「进程死活」判据下会白等到 cleanup，不会自动重启。

> settings 里存的是 **flag 原值**而非判死延迟：2× 是实测关系、可能随 ffmpeg 版本变，把它折进配置值会让配置静默撒谎。

## 抽帧与背压

入 `ca_ready` 走 `ClientQueues.append_ca_ready_with_throttle()`：整数降采样"**每 N 帧留 1**"（N=`inference_decimation`，默认 2）——`_decimate_counter` 计数，未满 N 丢弃、满 N 放行并归零，长期保留率精确 `= 1/N`。输入为 ffmpeg 规范化后的 CFR 流，CFR 已把时间烙成等距帧号，故整数计数天然精确均匀、**不依赖 wall-clock**（墙钟间隔门受解码线程调度抖动、稳定达不到目标抽帧率，故弃用），也无浮点相位累积。整数因子故**只命中整除比**（30→15/10/7.5…，不支持 30→20 类非整除比；非整除诉求由模型侧 `model_input_fps` 按 ts 重采样承接）。检测率 = `raw_fps/N`（默认 30/2=15，见 [app/settings.py](../../app/settings.py) 派生属性 `inference_fps`）。队列满则丢（背压只丢推理帧，`ca_raw` 录制继续）。`_decimate_counter` 仅由 decoder 线程读写、SPSC 无锁。decoder 读 `manager.get_pending_count(task_id)` 判背压——此跨模块读是有意保留。

> 采样旋钮的**唯一真源**是 `inference_decimation`（[app/settings.py](../../app/settings.py)）；`inference_fps` 是其派生 property（`raw_fps/N`），供 throttle 报告与 VizWorker target 消费（消费者继承采样流速率）。persistence processed 段编码**不再共用** `inference_fps`，改由帧 ts 逐段反推 `eff_fps`（见 [SERVICE_PERSISTENCE.md](SERVICE_PERSISTENCE.md)）。

## URL 重写

`_rewrite_rtsp_url()` 仅当 URL 端口 == `settings.mediamtx_proxy_port` 时生效：host 固定 `127.0.0.1`、port 改 `settings.mediamtx_internal_port`、保留 userinfo——后端拉流绕过 RTSPProxy 直连本机 MediaMTX 内部端口。

## 健康监控输入

健康监控的断流判据是 **decoder 子进程死活**（`is_decoder_alive`），`latest_raw_timestamp` 只用于「重连是否成功」与最后防线的无帧超时。重连接管在 GlobalHealthMonitor（见 [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)）：`start_stream` 失败后 decoder 仍留字典由监控异步重连，不再走 GuardedExecutor 双重重试。真·网络分区下把「挂死」转成「进程退出」的正是上面那条 `-timeout`，故这条判据的**感知延迟 = `2 × rtsp_read_timeout_s`**。

## 代码来源

- `app/services/stream/manager.py`（`StreamService`）
- `app/services/stream/decoder.py`（`_rtsp_input_opts` / `FFmpegDecoder`）
- `app/services/stream/instance.py`（单例）
- `app/services/stream/config.py`
- `app/settings.py`（`rtsp_read_timeout_s`）
- `config/stream_config.yaml`
- `tests/test_rtsp_read_timeout.py`
- `tests/test_stream_rewrite.py`
- `tests/test_reconnect_on_initial_failure.py`
