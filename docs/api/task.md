# `/task` — 前端消息、告警历史与大屏清单

四个 GET，分两组：

- **告警**：`message` 走**运行时内存增量**（前端轮询用），`{task_id}/alarms` 走**数据库历史**。
- **大屏清单**：`live` 出在线任务、`history` 出可回放的历史任务。两者**只出参数、不出播放 URL**——前端拿参数自己拼 `/ai/video`、`/traceback/*` 的地址。

通用约定见 [README](README.md)。

---

## GET /task/message/{task_id}

前端实时告警消息（按 seq 增量 + 近 10s 信号）。适合前端 1–2 Hz 轮询。数据来自内存（非 DB）。

**路径参数**：`task_id`（int）。
**查询参数**：`since_seq`（int，默认 0）—— 游标，只返回 `seq > since_seq` 的告警。`since_seq < 0` → **400**。

**200**：

```jsonc
{
  "task_id": 123,
  "max_seq": 42,                          // 当前最大告警 seq；下次请求带 since_seq=max_seq 取增量
  "signals_10s": {                        // 近 10s 按 metric 聚合的实时信号；键 = AlarmMetric 值（仅 realtime 规则的流）
    "BUBBLE": { "active": true, "hit_count": 3, "max_conf": 0.95 }
    // 含全量空模板：未命中的 metric 为 {active:false, hit_count:0, max_conf:0.0}
  },
  "alarms": [                             // seq > since_seq 的增量告警
    {
      "seq": 40,
      "mode": "REALTIME",                 // REALTIME | SETTLEMENT
      "metric": "BUBBLE",                 // AlarmMetric 值
      "level": "high",                    // low | medium | high | critical
      "message": "持续产生新气泡…疑似漏气",
      "ts": 1751800000000                 // epoch 毫秒
    }
  ]
}
```

**无活跃 run**：返回空模板 —— `max_seq: 0`、`alarms: []`、`signals_10s` 全零模板（不报错）。

> 游标用法：客户端保存返回的 `max_seq`，下次以 `since_seq=max_seq` 请求，避免重复。字段名较短（`metric`/`level`/`message`），与 `/admin` 的告警字段名不同。

---

## GET /task/{task_id}/alarms

该任务的**全部**告警历史（始终查 `clean_alarm` 表，DESC）。较实时告警有秒级延迟（AlarmWorker 异步写库）。

**路径参数**：`task_id`（int）。

**200**：

```jsonc
{
  "task_id": 123,
  "total": 5,
  "alarms": [
    {
      "alarm_id": 1001,
      "task_id": 123,
      "step_id": 10,
      "step_name": "泄漏检测",
      "alarm_type": "流程违规",           // 流程违规 | 任务超时 | mock_alarm
      "severity": "high",                 // low | medium | high | critical
      "message": "...",
      "resolved": false,
      "resolved_by": null,
      "detected_at": 1751800000000,       // epoch 毫秒，可为 null
      "resolved_at": null                 // epoch 毫秒，可为 null
    }
  ]
}
```

**错误**：`503`（DB 查询失败，retryable）。

---

## GET /task/live

在线任务清单（大屏用）。纯内存快照，零 DB、零磁盘，无查询参数。

**200**：

```jsonc
{
  "total": 2,
  "tasks": [
    {
      "task_id": 101,                       // → WS /ai/video?task_id=101
      "source_ip": "10.0.0.1",              // → WS /ai/video?client_id=10.0.0.1
      "step_id": 2                          // 当前洗消阶段，仅供展示，不参与画面路由
    }
  ]
}
```

**接实时画面**（两种模式见 [ai.md](ai.md)）：

| 用 | 语义 |
|---|---|
| `?task_id=` | 锁定**这一次 run**，run 结束即止、不跟随新任务。适合针对某次任务的监看 |
| `?client_id=<source_ip>` | 跟随该**点位**的当前 run，任务来了显示、走了黑屏、换 run 自动跟。适合固定点位常亮大屏 |

**无活跃 run**：`{"total": 0, "tasks": []}`（不报错）。

> 与 `/admin-f3m8/clients` 同一份注册表快照，本接口是大屏版——去掉队列深度等运维字段。

---

## GET /task/history

历史任务清单（大屏用）：最近 **10** 个**已完成且能回放**的任务，按最近有画面倒序。无查询参数。

**「已完成」判定**：磁盘上有段（能播）**且** 不在活跃注册表里（跑完了）。刻意**不看** `clean_task.status`——该字段由平台业务侧写入，取值集合后端无从校验；拿它过滤等于把清单挂在未知字面量上，写错就静默变空。

**200**：

```jsonc
{
  "tasks": [
    {
      "task_id": 101,
      "source_ip": "10.0.0.1",              // DB 补；DB 不可用或表里无此任务 → null
      "latest_ms": 1700000600000,           // 最近一次有画面的时刻，也是本清单的排序键
      "steps": [                            // 时间字段只在 step 粒度给，见下方说明
        { "step_id": 1, "tracks": ["raw", "processed"], "start_ms": …, "last_segment_ms": … },
        { "step_id": 2, "tracks": ["raw"],              "start_ms": …, "last_segment_ms": … }
      ]
    }
  ]
}
```

**接历史画面**（详见 [traceback.md](traceback.md)）：

```
GET /traceback/task/{task_id}/playlist.m3u8?step_id={step_id}&track={track}
GET /traceback/task/{task_id}/timeline?step_id={step_id}
```

> ⚠️ **`track` 必须从 `steps[].tracks` 里挑**。playlist 的 `track` 默认 `processed`，而只落了 raw 的 step 照默认打过去就是 **404**。

**时间字段只给到 step 粒度**，任务级不给 `start_ms`：

- 回放本身就是 step 粒度（playlist 必填 `step_id`，跨 step 聚合不支持），且**两个 step 之间可以隔任意长时间**。任务级的「最早 ~ 最晚」会跨过中间空档，既不是任务时长、也不对应任何可播放的东西，只会被误读成连续区间。
- 任务级因此只留 `latest_ms`（= `max(steps[].last_segment_ms)`），用途明确：清单排序键 + 「这是什么时候的任务」的展示值。

`steps[]` 里那对时间的细则：

> `start_ms` / `last_segment_ms` 是 **raw 与 processed 双轨的并集**，与 timeline 的 `start_ms`/`end_ms` 同口径——两轨段边界不一定对齐，某一轨的实际范围可能比这里窄（实测有过 20+ 秒的差）。它表达的是「这个 step 有画面的时间跨度」，别拿它跟单轨播放进度做逐帧对齐。
>
> `last_segment_ms` 是最后一段的**起点**，比 step 真正结束早一个段长（`last_segment_ms + 该段 EXTINF = timeline 的 end_ms`）。要精确时长用 timeline 的 `duration_ms`。

**降级**：DB 不可用时仍返回 200，`source_ip` 全为 `null`——清单的存在性判定来自磁盘，DB 只补点位显示，不因此 503。

**不返回 `total`**：固定 10 条，无翻页语义。要带过滤/分页的完整任务列表用 [`GET /lab-f3m8/tasks`](lab.md)（送标页口径，只枚举 raw 轨）。
