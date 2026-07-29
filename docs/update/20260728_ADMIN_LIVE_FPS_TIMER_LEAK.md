# admin live fps 徽章跳变：根因是前端定时器泄漏，非管线掉帧

> **变更状态**：生效中（2026-07-28）
> **知识库**：待沉淀 → kb/SERVICE_INFERENCE.md（[VIZ_THROUGHPUT] 一节的排查案例）
> **相关**：issue #82

## 结论

`admin` 实时监控的 fps 徽章在双客户端下跳变（15→3、后续复现为 0→10），**与推理/渲染/推送链路无关**。根因是 [app/static/admin/index.html](../../app/static/admin/index.html) 的 `stopLive()` 漏清 `fpsTimer`：

```js
// startLive() —— 每次调用都无条件新建一个 interval
fpsTimer = setInterval(() => { liveFps.value = fpsFrameCount; fpsFrameCount = 0; }, 1000);

// stopLive() —— 只清了 alarmPollTimer，fpsTimer 从头到尾没有任何 clearInterval
if (alarmPollTimer) { clearInterval(alarmPollTimer); alarmPollTimer = null; }
```

切一次客户端就走一遍 `onClientChange → stopLive → startLive`，泄漏一个定时器。N 个定时器相位错开地对**同一个** `fpsFrameCount` 做「读出 → 归零」：谁先醒谁把计数抢走，后醒的读到残值甚至 0。于是徽章在 0 与真值的某个分数之间跳，而**画面始终流畅**——因为真实推送一直是满额 15fps。

修法：`stopLive()` 补 `clearInterval(fpsTimer)`；`startLive()` 起表前把 `fpsFrameCount` 归零，免得上一轮的残帧算进新连接的第一秒。

## 排查中被证伪的假设（留档，避免重走）

| 假设 | 证伪依据 |
|---|---|
| 组批 + `_latest_inference` 单槽覆盖，同一 cq 多帧落进同一批被去重 | `[PRESSURE] resource=stage_queue` 恒 `depth=0 drop_total=0` → deque 从不积压 → 批恒为 1，无覆盖 |
| 推理子进程吞吐不足 | 同上；且 decoder `received 300 frames (raw=300, ready=150, dropped=0)` 双路零丢 |
| viz 单线程被 GIL 饿着 | `ticks=289~297/300`，转满 |
| 渲染慢（render-bound） | `render=12~15ms(max 42ms, budget 67ms)` |
| 写回被 `cq.is_active()` 门挡 | 双路并行期 `out=14.7fps`，满额 |
| WS 上行带宽打满 | 两条 live view 全程连着的 45s 内 `[VIZ_THROUGHPUT]` 全程静默（即两路 out 均 ≥12fps），且用户观测到画面不卡 |

## 副产品

排查过程中给 `[VIZ_THROUGHPUT]` 补的观测正是把上述服务端假设逐条排掉的依据，见
[20260728_VIZ_TICK_HEALTH_AND_SPAN_BASIS.md](20260728_VIZ_TICK_HEALTH_AND_SPAN_BASIS.md)
与 [20260728_PRESSURE_REASON_REFLECTS_TRIGGER.md](20260728_PRESSURE_REASON_REFLECTS_TRIGGER.md)。

## 排查中撞见但**不是**缺陷的一点

viz 取 `cq.get_latest_frame()`（最新原始帧）叠加检测结果、并盖 `inference.ts`
（[worker.py](../../app/services/inference/visualization/worker.py) `_process_client`）——
积压时框会滞后于画面。这是**有意设计**，不要当 bug 去"修"成按 ts 配对：画面优先取最新帧
保证实时观感。留档以免下次排查时被当成线索。

## 遗留（未修，另计）

- **单帧渲染峰值**：观测到过 `max 132.6ms`（均值 13~15ms），远未触 67ms 预算的告警线但值得留意。
