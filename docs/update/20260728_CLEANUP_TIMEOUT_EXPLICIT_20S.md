# cleanup_timeout 提成显式配置并改 20s（删「次数×间隔」派生式）

> **变更状态**：生效中（2026-07-28）。行为变更：放弃重连的时限 30s → **20s**。
> **知识库**：已就地更新 `kb/SERVICE_HEALTH_MONITOR.md` 的清理条件一节。
> **前置**：[20260726_RECONNECT_PROCESS_LIVENESS.md](20260726_RECONNECT_PROCESS_LIVENESS.md)（判据改进程死活、删重连次数上限）。

## 改了什么

两件事，一次做完：

1. `cleanup_timeout` 从**派生量**变成**一等配置项**，默认 **20.0 秒**（原派生值 30s）。
2. 删掉 `max_reconnect_attempts`（配置字段 + monitor 属性 + yaml 键 + `/health/monitor/config` 回显）。

```python
# 旧（monitor.py）：一个纯时间阈值，用「次数×间隔」表达
self.cleanup_timeout = heartbeat_timeout + reconnect_interval * max_reconnect_attempts  # 5 + 5×5 = 30

# 新（config.py 直配、monitor.py 直读）
cleanup_timeout: float = 20.0
```

## 为什么删派生式，而不是把系数从 5 调成 3

前置改动已经把「重连次数上限」这个概念删掉了——decoder 进程死就按 `reconnect_interval` **无限** respawn，只由无帧时长收口。此后 `max_reconnect_attempts` 就只是个换算系数，但名字仍在说「最大重连次数」：**改它的人以为在调重连次数，实际是在调放弃时限**。调 5→3 能得到 20s，但会把这个误导再固化一轮，且 20 这个目标值散成两个数的乘积、看不出来。

同一条口径此前已在别处用过（[20260727_CA_QUEUE_DEPTH_AND_VALIDATION.md](20260727_CA_QUEUE_DEPTH_AND_VALIDATION.md) 删 `ca_maxlen < 300` 的绝对帧数魔数）：**一个阈值该用它自己的量纲直接声明**，不借另一组旋钮的乘积表达。

## 20s 是什么权衡

`cleanup_timeout` 从**最后一帧**算起，超时即放弃重连、清理整个 run（停 decoder、结算 HLS/告警、清 registry）。

- 调大 = 容忍更长的现场中断（摄像机重启、网络抖动），代价是死流占资源更久；
- 调小 = 更快释放资源，代价是把「本可恢复的中断」升级成「任务终止」。

20s 意味着**现场中断超过 20 秒即视为不可恢复**。这是个运营口径，不是技术下限——若现场出现「摄像机重启约 25s、任务被误清」的情况，改 yaml 一个数即可。

## 影响面

| 文件 | 改动 |
|---|---|
| `app/services/health_monitor/config.py` | 删 `max_reconnect_attempts`，加 `cleanup_timeout: float = 20.0`（yaml 键同名） |
| `app/services/health_monitor/monitor.py` | `self.cleanup_timeout` 直读配置；删 `self.max_reconnect_attempts` |
| `config/health_monitor_config.yaml` | 换键，注释写明「重连不数次数，只由本项收口」与调大/调小的含义 |
| `app/routers/health.py` | `/health/monitor/config`：`config` 段 `max_reconnect_attempts` → `cleanup_timeout`；**删掉整个 `derived` 块** |
| `tests/`、`integration_tests/`、`kb/SERVICE_HEALTH_MONITOR.md` | 常量与叙述从 30s 改 20s；集成测试 `AUTO_CLEANUP_TIMEOUT` 45→35 |

**对外契约变更**：`/health/monitor/config` 响应少了 `config.max_reconnect_attempts`，且 `derived` 块整块删除——`cleanup_timeout` 提成一等配置后，该块两个值都退化成 `config` 的恒等副本（`suspect_timeout` ≡ `heartbeat_timeout`），同一响应回显两遍纯属冗余。全仓无消费者（已 grep `integration_tests/`、`app/static/`、`tests/`），该端点只供运维手查。

集成测试的两个时间常量仍在窗口内（`RECONNECT_GAP`=10s、`DELAYED_STREAM_DEFAULT`=10s，均 < 20s），无需改。全套 391 passed。
