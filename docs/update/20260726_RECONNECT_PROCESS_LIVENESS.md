# 重连判据：帧 staleness → decoder 进程死活（根治启动延迟翻倍）

> **变更状态**：生效中（2026-07-26）
> **知识库**：已沉淀 → [SERVICE_HEALTH_MONITOR](../kb/SERVICE_HEALTH_MONITOR.md)（断流与重连判据 = decoder 进程死活）、[SERVICE_STREAM](../kb/SERVICE_STREAM.md)（_RTSP_INPUT_OPTS：tcp + -timeout）(2026-08-02)

日期：2026-07-26
影响模块：health_monitor、stream(decoder/service)、集成/单元测试

## 背景问题

`test_single_client` 实测任务启动延迟 ~9.7s。逐层实验（含 mediamtx + ffmpeg 推/拉 三进程隔离台）定位到两层叠加根因：

1. **源 GOP 过大**：推流无 `-g`，x264 默认 keyint=250(~8-10s)。RTSP reader 从关键帧起收，平均等 ~5s = 首帧地板。
2. **健康监控误杀首连**（真凶）：监控用 `latest_raw_timestamp` staleness 判死（`heartbeat_timeout=5s`），把「正等首个关键帧」的 reader 当掉线 kill 掉、逼出重连、再等一个 GOP → 启动延迟翻倍到 9.7s。

三进程实验决定性结论：**publisher 断开时（SIGTERM 优雅停 / SIGKILL 硬断都一样）后端 puller ffmpeg 立即读到 EOF 并退出（exit 0），不会挂死**——RTSP 控制通道恒是 TCP，MediaMTX 拆掉 reader 会话后 ffmpeg 从控制通道收到 EOF。

## 改动

**判据从「帧是否停」改为「decoder 子进程是否活」**（`is_decoder_alive`）：

- 进程死（断流 EOF / 崩溃 / 首启失败）→ 进入重连、按 `reconnect_interval` 节流反复 respawn；
- 进程活但无帧（等首帧 / 瞬时停）→ **只等不杀**（根治启动 bug）；
- 放弃(cleanup) 改纯时间触发：无帧 ≥ `cleanup_timeout`（=30s），**删除重连尝试计数**（`max_reconnect_attempts` 只作 cleanup_timeout 派生系数）；
- 成功 = 真来新帧（非「进程活」），避免 respawn 后等首帧的活进程被再杀。

**decoder（`_RTSP_INPUT_OPTS`）**：
- `-rtsp_transport` udp→**tcp**：消除 UDP 独立 RTP 端口建连竞态（否则「进程活但 0 RTP」在新判据下会白等到 cleanup）；loopback 无延迟损失、与推流端一致。
- 新增 **`-timeout 5000000`**（rtsp demuxer socket 读超时，微秒）：让「真·网络分区」下 socket 无数据也不 FIN 的罕见挂死主动超时退出（→ 进程死 → 被重启）。实验台确认该 flag 有效（`-rw_timeout` 作 rtsp 输入选项在本 ffmpeg 8.1.1 被拒）。

**新接口**：`StreamService.is_decoder_alive(task_id)`（对标 `get_stream_info`，锁序 StreamService.lock→decoder.lock）。

**保留的对外契约**：`/health/status` 各字段、`reconnecting_clients`、日志串 `RECONNECT MODE/ATTEMPT/SUCCESS/FAILED`、`max_reconnect_attempts` 配置键（重解为 cleanup_timeout 系数）。

**测试**：`stream_stabilize` 恢复 5s；推流保留 `-g 30`（小 GOP 逼近低延迟实况）；`tests/test_reconnect_on_initial_failure.py` 按新判据改写；scenario 3/6 期望日志串/时间常量注释按「时间触发 cleanup」重述。

**附带观测**：新增启动延迟三段埋点 logger `app.startup_latency`（`first_frame`/`first_inference`/`first_rendered`，相对 `cq.task_started_at`），见 `queues.mark_startup_milestone`。

## 已知正交遗留（未修）

`/health/status` 实际不返回 `queues` 键、且 `reconnecting_clients` 为 int task_id 而集成测试用 str client_id 比对——使部分集成测试观测形同虚设，属既有 bug，与本次无关。
