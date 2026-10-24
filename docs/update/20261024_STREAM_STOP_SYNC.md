# stream 停流改同步：删除 stop-decoder 线程，`stop_stream` 返回即已回收 ffmpeg

> **变更状态**：生效中（2026-10-24）
> **知识库**：待沉淀

## 概述

`StreamService.stop_stream` 不再起 `stop-decoder-{tid}` 线程异步停 decoder，改为锁外同步 `decoder.stop()`：返回时 ffmpeg 已被杀并回收、读线程已 join。
`start_stream` / `stop_stream` docstring 注明只由 run_control 持任务锁调用。对外接口、配置、日志不变。

## 变更背景

- **异步的由来**：2026-01 时 `decoder.stop()` 是 `terminate → wait(2s) → kill`，且在 `StreamService.lock` 内执行；ffmpeg 卡在死 socket 上收不到 SIGTERM，
  每次白等 2s 以上，清理流程与 health_monitor 被卡住，于是改为后台线程停。
- **理由已失效**：2026-06 `decoder.stop()` 改为直接 SIGKILL（阻塞态 2007ms → 2ms）；2026-07 `restart_stream` 已改回同步停。只剩 `stop_stream` 仍异步。
- **实测**（Windows 开发机，直接调 `FFmpegDecoder.stop()`，自起 mediamtx + 720p30 推流，本地代理冻结转发模拟死 socket）：

  ```text
  场景                                  n    median    p95     max
  正常出帧中停                          20    5.0ms   19.7ms  19.7ms
  出帧后冻结 1s（卡在死 socket 读）     20    5.3ms   16.0ms  16.0ms
  握手阶段冻结（RTSP 无应答）           20    3.2ms   12.1ms  12.1ms
  进程已自行超时退出                     5    0.1ms    0.2ms   0.2ms
  15 路并发停                         3×15    ~9ms    ~12ms   12.6ms
  ```

  Linux 沿用 2026-06 审计数据：SIGKILL 2ms、真实流 `/terminate` 全程 12.9ms。

## 方案详情

- [`stop_stream`](../../app/services/stream/service.py)：锁内只 pop，锁外同步 `dec.stop()`；删除 `_stop_decoder_async`。
  `decoder.stop()` 的异常不再被后台线程吞掉，交给调用方：`stop_run` 记入 `errors`（`decoder: …`），health_monitor 停孤儿路径已有 try。
- 不放进全局锁：`decoder.stop()` 三段各有 2s 超时（回收进程、join reader、join stderr），最坏约 6s，只拖住本 task 的任务锁。
- 无死锁：reader 写帧路径（背压查询读 COW 注册表、CQ append 非阻塞）不取任务锁也不取 `StreamService.lock`。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `stop_run` 走到 `flush_residual` / 注销 CQ 时 | 旧 reader 可能仍在写，靠 DRAINING 门挡迟到帧 | 生产者已确定停止，DRAINING 门只作兜底 |
| `stop_run` 结果 `decoder_stopped` | 后台线程已发起 | 进程已杀并回收 |
| `/start` 换代 | 新旧 ffmpeg 可能同时连 MediaMTX 同一路径 | 旧的先断，新的再连 |
| 进程关停 | `run_control.shutdown` 起的 daemon 停止线程与解释器退出赛跑 | 返回时 ffmpeg 已全部回收 |
| 停 decoder 的写法 | `stop_stream` 异步，`restart_stream` / `shutdown` 同步 | 统一同步 |

**自测结果**

| 项 | 用例 | 结果 |
|----|------|------|
| 同步停、不持全局锁、异常上抛 | `tests/test_stream_stop_sync.py`（修复前 2 条均失败） | 2 passed |
| 全量 `pytest tests/` | — | 1122 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `start_stream` 与 restart 第二段锁在 `StreamService.lock` 内调 `dec.start()`（含 Popen + 固定 `sleep(0.1)`） | 每次起流持全局锁 ≥100ms，15 路同时起流排队，health_monitor 读跟着等；非正确性问题 | 15 路验收看到排队延迟再处理：`start()` 挪出锁需配同样的身份核对 |
| dev 环境真流端到端（拆除、换代、断流重连）未在本批重跑 | 同步停仅以 decoder 级实测与单测覆盖 | 合入前在 dev 跑一遍 |
