# 健康监控清死状态：只写不读的 dict、死字段、说谎的统计名与日志

> **变更状态**：生效中（2026-07-29）。**零行为变化**，改的全是没人读的状态和名不副实的输出。
> **知识库**：`kb/SERVICE_HEALTH_MONITOR.md` 无需改（描述的判据与清理条件均未变）。
> **背景**：判据从帧 staleness 改进程死活（[20260726](20260726_RECONNECT_PROCESS_LIVENESS.md)）、
> cleanup_timeout 提成显式配置（[20260728](20260728_CLEANUP_TIMEOUT_EXPLICIT_20S.md)）之后留下的残渣。

## 1. `_last_activity`：只写不读，且会漏

```python
last_frame_time = cq.latest_raw_timestamp
if task_id not in self._last_activity:
    self._last_activity[task_id] = last_frame_time    # 写
idle_time = current_time - last_frame_time            # ← 用的是局部变量，不是这个 dict
```

全类**没有任何一处读取** `_last_activity`，它只被写入和 `pop`。而 `pop` 只在两条清理路径上，
孤儿流后来恢复了的 task 条目永久滞留 → 随进程生命期按 task_id 累积。

它想表达的「最后活跃时间」本来就是 `cq.latest_raw_timestamp` 本身，另存一份只是多一处要同步的
状态。整个字段删除，空闲时长直接由帧 ts 算。

## 2. `ReconnectState.attempt_count`：死字段 + 注释描述已不存在的机制

本轮改造删掉了 `state.attempt_count += 1` 和「次数耗尽即放弃」的逻辑（放弃改由
`cleanup_timeout` 纯时间收口），字段只剩构造时置 0，无人读。更糟的是 `types.py` 的注释还写着
「attempt 耗尽 → stop_run(expected=cq)」——描述的是已经删掉的判据。字段与注释一并清，
剩下两个字段补上各自用途的行内说明。

## 3. `_stats["suspects"]`：名字与语义脱节

"suspect"（可疑区间）这个概念随判据改造删除，计数器却留着，实际统计的是「进入重连模式的次数」。
改名 `disconnects`，并把三个重连计数摆成一条可读的链：

| 键 | 含义 |
|---|---|
| `disconnects` | 检测到 decoder 进程死、进入重连模式的次数（含首启失败） |
| `reconnects` | 发起 respawn 的次数 |
| `reconnect_successes` | 真来新帧、判定恢复的次数 |

**对外契约变更**：`/health/monitor/stats` 与 `/health/status` 的 `monitor_stats` 里
`suspects` → `disconnects`。admin 面板只读 `checks` / `reconnect_successes`，集成测试只读
`reconnecting_clients`，均不受影响（已 grep `app/static/`、`integration_tests/`）。

## 4. 两条说谎的日志

- **`_handle_task_timeout` 把同一个值打了两遍**：`client=%s, task_id=%s` 传的都是 `task_id`
  ——`client_id`/`task_id` 合一后的残留，读日志的人会以为是两个维度。同时函数体开头
  `task_id = cq.task_id` 把入参当场覆盖（值相同，纯噪音），一并删。
- **启动行打了两条，其中一条内容已失效**：`GlobalHealthMonitor.start()` 和 `health.py` 的
  `lifespan` 各打一条 `[GlobalHealthMonitor] Started`。后者报的 `timeout=heartbeat_timeout`
  在「可疑」判据删除后已不对应任何生效的判定。删 lifespan 那条，保留 start() 里报
  `cleanup_timeout` 的那条（这条线上唯一还在做判定的时限）。

顺带修正 `/health/status` docstring 里「check_interval 默认 5 秒」——实际默认 1.0s。

## 验证

纯删除与重命名，无逻辑分支改动。全套 391 passed。
