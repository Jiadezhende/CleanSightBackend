> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Stream Service

每个 run 一个 FFmpeg 解码器，把帧写进该 run 的 ClientQueues（下称 CQ）。运行键 = int `task_id`；系统只用 RTSP。

`StreamService` 只是 decoder 注册表 + 生命周期编排：读帧在 decoder 自持线程里，服务侧没有轮询线程，构造无副作用。类在 `service.py`，单例 `stream_service` 在 `instance.py`；`__init__.py` 只有 `lifespan()`（无启动段，finally 里调 `shutdown()`）。CQ 只取不建：`client_service.get(task_id)` 取不到时记 error、decoder 空跑（说明调用序错了）。

## StreamService 公开方法

| 方法 | 语义 |
| ---- | ---- |
| `start_stream(task_id, url)` | 先注册 decoder 再同步 `start()`。同 task 已有存活 decoder → `ConflictError`；已死的先清掉。首次 `start()` 失败**不抛**，decoder 留在字典里，由健康监控下个 tick 重连 |
| `stop_stream(task_id)` | 从字典弹出后交 daemon 线程异步 `stop()`，不阻塞调用方；迟到帧由 CQ 写门（DRAINING/CLOSED）拦下 |
| `restart_stream(task_id, url) → bool` | 锁外同步停旧 → 锁内清旧、建新、`start()`，复用现有 CQ。旧 reader join 完新 reader 才写 `ca_ready`，保住 SPSC 单生产者，也避免新旧进程在 MediaMTX 同路径上抢连接。吞掉所有异常、失败返回 False，不阻塞健康监控线程 |
| `is_decoder_alive(task_id)` | decoder 子进程是否存活，即健康监控的断流判据 |
| `get_all_task_ids()` | 已**注册**的 task_id，不看死活——「已死待重连」的 decoder 必须留在里面 |
| `get_stream_info(task_id)` | `{"url": 重写后的拉流地址}` 或 None；重连只需 url |
| `get_pending_count(task_id)` | `ca_ready` 深度，供 decoder 准入背压 |
| `shutdown()` | 摘走全部 decoder 后逐个同步 `stop()`。停机时各 run 的 decoder 已由 `run_control` 拆掉（见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)），这里只收不属于任何 run 的残留 |

## FFmpegDecoder：同步起、自持读线程、SIGKILL 停

- **组合而非继承**：decoder 持有 reader 线程、自身不是 `Thread`，因为 `start()` 必须同步完成 Popen 与秒退检测并当场抛异常——健康监控靠这个同步失败信号首次感知。
- **`start()`**：Popen 后等 0.1 s 看是否秒退；秒退则 `wait(1.0)` 回收僵尸，按 stderr 标记（404 / not found / connection refused 等网络类）抛 `StreamConnectionError`（可重试），否则抛 `FFmpegError`（致命）；二进制不存在也抛 `FFmpegError`。
- **`_reader_loop`**：阻塞读 stdout，双平台同一路径；开头捕获 stdout 本地引用，避开 `stop()` 置 `proc=None` 的 TOCTOU；读到 `b""` 或 `ValueError` 即正常退出，不自行重启。
- **`stop()`**：进程活着就直接 SIGKILL，再无条件 `wait(timeout)` 回收、关管道，锁外 join 两个线程。只解码到 pipe、无产物可损坏，卡在死 socket 上的 ffmpeg 又收不到 SIGTERM，优雅等待只会白耗 ~2 s。
- **输出规范化为 CFR**：`-vf scale=W:H,fps=raw_fps -vsync drop`，rawvideo bgr24、默认 640x480；帧率只取 `settings.raw_fps`，yaml 不写 fps。
- **帧与去向**：`Frame.timestamp = time.time()`（读到帧的墙钟时刻）；写 `ca_raw`（全帧率）、`ca_ready`（经准入背压与抽帧）、`latest_raw_frame / latest_raw_timestamp`。
- `settings.ffmpeg_path` 与 `_rtsp_input_opts()` 每次建命令时读，改 env 不用重启进程。

## 准入背压只丢推理帧，录像照写

decoder 每解析出一帧，经 `manager.get_pending_count()`（manager 即 StreamService，这条回读是有意保留的）取 `ca_ready` 深度。占用率 ≥ `backpressure_ratio`（默认 0.90，`config/stream_config.yaml`）时，该帧不进 `ca_ready`，计 `frame_drop_total{reason="ingress_backpressure"}`，每丢 100 帧打一条 DEBUG `[BACKPRESSURE]`；`ca_raw` 照写。解析异常计 `reason="decode_error"`。进 `ca_ready` 之后的整数抽帧与队满兜底在 CQ 内，见 [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)。

## 拉流读超时：判死延迟是 `-timeout` 的 2 倍

`-timeout T` 是 socket 读超时，取自 `settings.rtsp_read_timeout_s`（默认 2.5 s，env `CLEANSIGHT_RTSP_READ_TIMEOUT_S`）。

- **实际判死延迟是 `2T`**：ffmpeg 第一次读超时不致命、会重试一次，第二次才退出。这是 demux 层行为，去掉 `-err_detect ignore_err`、`-fflags nobuffer+discardcorrupt` 乃至只留 `-rtsp_transport tcp -timeout` 都一样。实测（ffmpeg n7.1.4，中继停止双向转发但不发 FIN）：

  ```text
    -timeout    首次触发               退出                    比值
      2.0s      —                      4.50s                   2.25×
      2.5s      2.75s                  5.41s                   2.16×
      5.0s      5.11 / 5.12 / 5.26s    10.51 / 10.56 / 10.42s  2.10×
     10.0s      —                      20.28s                  2.03×
  ```

  比值随 T 增大收敛到 2：两次等待加常数开销，没有第三次。
- **两次超时之间可恢复**：冻结跨过第一次超时、在第二次之前解冻，关键帧一到立即恢复，进程不退。
- **`2T` 必须明显小于健康监控的 `cleanup_timeout`**（默认 20 s），否则进程还没退就被 cleanup 拆掉，重连永远轮不上。预算关系与越界告警见 [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)。
- **`-rtsp_transport tcp` 不可换 udp**：UDP 下「会话建成却 0 RTP」时进程不退，只能白等到 cleanup。
- **settings 存 flag 原值而非判死延迟**：2× 是实测关系、可能随 ffmpeg 版本变，折进配置值会让配置静默失真。

## URL 重写：后端拉流绕过 RTSPProxy

`_rewrite_rtsp_url()` 仅当 URL 端口等于 `settings.mediamtx_proxy_port` 时生效：host 改 `127.0.0.1`、端口改 `settings.mediamtx_internal_port`、保留 userinfo，直连本机 MediaMTX 内部端口。

## 代码来源

- `app/services/stream/service.py`（`StreamService`、`_rewrite_rtsp_url`）
- `app/services/stream/decoder.py`（`_rtsp_input_opts` / `FFmpegDecoder`）
- `app/services/stream/{__init__,instance,config}.py`、`config/stream_config.yaml`
- `app/settings.py`（`raw_fps`、`rtsp_read_timeout_s`）
- `tests/test_rtsp_read_timeout.py`、`tests/test_stream_rewrite.py`、`tests/test_reconnect_on_initial_failure.py`
