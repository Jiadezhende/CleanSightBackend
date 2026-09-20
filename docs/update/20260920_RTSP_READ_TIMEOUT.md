# 拉流读超时：判死延迟是 `-timeout` 的 2 倍，默认值改 2.5s 并收进 settings

> **变更状态**：已实现（2026-09-20）
> **知识库**：待沉淀 → `SERVICE_STREAM.md`（`_RTSP_INPUT_OPTS` 那条）、
> `SERVICE_HEALTH_MONITOR.md`（断流与重连判据）
> **前置结论来自**：[20260726_RECONNECT_PROCESS_LIVENESS.md](20260726_RECONNECT_PROCESS_LIVENESS.md)
> （重连判据 = decoder 进程死活；`-timeout` 就是那一轮加的）

## 结论

1. `-timeout T` 的判死延迟是 **2T**，不是 T。ffmpeg 第一次读超时**不致命**，重试一次、第二次才退出。
2. 线上原配 5s ⇒ 静默断流实际 10.5s 才判死，吃掉 `cleanup_timeout`(20s) 一半以上的重连预算。
3. 默认值改 **2.5s**（判死 ~5.4s），值收进 `settings.rtsp_read_timeout_s`，并加一条串联预算护栏。

## 1. 实测：超时准时，但要连续两次

ffmpeg **n7.1.4**，三进程台：publisher → MediaMTX → **可冻结 TCP 中继** → puller。中继到点
停止双向转发但**不 close、不发 FIN**——这是"真·网络分区"，不是推流端正常退出（那种 MediaMTX
会拆会话、控制通道给 EOF，ffmpeg 立刻退）。puller 用 `_rtsp_input_opts()` 的原样参数。

```text
-timeout 5s，debug 日志（时间戳相对冻结时刻）：
  +5.11s  Failed reading RTSP data: Error number -138   ← 准时触发，仅此一行，解码继续
  +10.22s Failed reading RTSP data: Error number -138   ← 第二次才升级
  +10.22s Error during demuxing → Terminating thread → EOF → 退出 rc=0
```

| `-timeout` | 首次触发 | 退出 | 比值 |
|---|---|---|---|
| 2.0s | — | 4.50s | 2.25× |
| 2.5s | 2.75s | 5.41s | 2.16× |
| 5.0s | 5.11 / 5.12 / 5.26s | 10.51 / 10.56 / 10.42s | 2.10× |
| 10.0s | — | 20.28s | 2.03× |

比值随 T 增大收敛到 2 ⇒ 是**两次等待 + 常数开销**，没有第三次。

**不是我们的 flag 造成的**：逐个去掉 `-err_detect ignore_err`、`-fflags nobuffer+discardcorrupt`
后行为不变，连只剩 `-rtsp_transport tcp -timeout` 的最小组合也是 2×。ffmpeg demux 层自带的重试，
配置改不掉。

**第一次超时之后还能恢复**（冻结 7s 后解冻，跨过第一次未到第二次）：

```text
  +5.12s  Failed reading RTSP data: -138
  +7.02s  nal_unit_type: 5(IDR)     ← 解冻，关键帧立刻恢复
  +10.33s 仍在解码，无第二次超时，进程活着
```

所以真实语义是「**连续 2T 收不到任何字节才判死**，中途恢复完全免疫」。抗一次时长在 T~2T
之间的瞬时卡顿，这是有用的性质，不是 bug。

**旧注释的两处不符**：说 ffmpeg 报 `"Operation timed out"`——本版只打
`Failed reading RTSP data: Error number -138`（`-138` = `ETIMEDOUT`），按文本 grep 日志的做法会落空；
说"取值 5s…远小于 cleanup_timeout(20s)，留足重启窗口"——按 2T 算余量只剩 8.5s。

顺带：退出码是 **0** 而非错误码。无影响——`is_decoder_alive` 只看 `dec.is_alive()`。

## 2. 为什么 2T 全记在 cleanup_timeout 账上

`cleanup_timeout` 从**最后一帧**算起，不是从进入重连算起
（`manager.py` 的 `idle_time = now - cq.latest_raw_timestamp`）。于是两种断法的重连窗口差一倍：

```text
cleanup_timeout = 20s，从最后一帧算起

正常 EOF 断流   进程即刻死 → ≤1s 进重连   → 剩 ~19s  给 respawn+建连+等 GOP
静默分区(T=5s)  +10.5s 才死 → ≤11.5s 进重连 → 剩  ~8.5s
静默分区(T=2.5s) +5.4s 才死 → ≤6.4s 进重连  → 剩 ~13.6s   ← 本次
```

**后果**：源在 10~20s 之间恢复时，同一次断流，EOF 断法能重连上，静默断法被判 RECONNECT
FAILED 并拆除整个 run（`cleanup=True`，等同 terminate）。

## 3. 改动

| 文件 | 改了什么 |
|---|---|
| [`settings.py`](../../app/settings.py) | 新增 `rtsp_read_timeout_s: float = 2.5`（秒；env `CLEANSIGHT_RTSP_READ_TIMEOUT_S`）。**存 flag 原值不存判死延迟**：2× 是实测关系、可能随 ffmpeg 版本变，藏进代码会让配置静默撒谎 |
| [`stream/decoder.py`](../../app/services/stream/decoder.py) | 模块级常量 `_RTSP_INPUT_OPTS` → 函数 `_rtsp_input_opts()`，`-timeout` 按调用取 settings（import 期定死的话改 env 要重启才生效）。整段注释按实测重写 |
| [`health_monitor/manager.py`](../../app/services/health_monitor/manager.py) | `start()` 末尾加 `_warn_if_read_timeout_starves_reconnect()`：`2T ≥ cleanup_timeout` 报 error，`2T` 吃掉过半预算报 warning |
| [`tests/test_rtsp_read_timeout.py`](../../tests/test_rtsp_read_timeout.py) | 新增 9 条：flag 取值来源与单位、按调用取值、tcp 不可协商、预算护栏三档边界 |

**护栏只告警不纠正**：两个值分居两处配置（读超时在 settings，`cleanup_timeout` 在
`health_monitor_config.yaml`），各有正当的运维理由，代码没资格替人选。但它们串联，越界时
**全程无一条日志**——进程还没死就先被 cleanup 拆除，重连永不触发。

## 4. 本次接受的代价

| 项 | 影响 | 为什么接受 |
|---|---|---|
| 抗抖动窗口从「静默 5~10s 可恢复」缩到「2.5~5s 可恢复」 | 一次 6s 的瞬时卡顿，以前能扛过去，现在会杀进程重连（丢一个 GOP） | 换来重连预算从 8.5s 回到 13.6s。巡检场景下"断了能自己接回来"比"扛过长抖动"重要；真需要长抖动容忍就调 env |
| 2× 关系写在注释里，不在代码里 | 换 ffmpeg 版本后注释可能失真 | 见上表第一行的取舍理由。护栏用的是 `2×`，换版本后护栏跟着失真——但它本就是保守告警，不是精确判据 |
| 实验台未进仓库 | 这一组数据无法一键复现 | 要跑真 MediaMTX + 两个 ffmpeg，不适合进 `tests/`。装置见下 |

## 5. 遗留 / 后续

- **实验台没落地**：`freeze_relay.py`（可冻结 TCP 中继）+ `run_trace.py`（带相对时间戳的
  stderr 追踪）目前只在临时目录。它能复现一类单测覆盖不到的场景（静默分区），若要留，
  合适的位置是 `integration_tests/` 旁边的手动装置，不是 `tests/`。
- **`heartbeat_timeout` 名不副实**：它早已不做断流判定，只剩"重连成功时新帧的新鲜度阈值"
  一个用途（`manager.py`），但名字还在暗示心跳超时。改名是纯清理，未做。
- **"进程活着、socket 有数据但不出帧"那一档仍是拆除而非重连**：`-timeout` 兜不住
  （socket 上有字节流动就不触发），落到 `cleanup_timeout` 的最后防线，走 `cleanup=True`。
  这一档也不会登记断流残帧 flush，横跨空洞的慢放段会长回来。未触发过，未修。
