# `/task` — 前端消息、告警历史与大屏清单

四个只读 GET，分两组。

**告警**（单个清洗任务，运行键 `task_id`）：

- `GET /task/message/{task_id}` —— 走**运行时内存增量**，前端轮询用；无活跃 run 也返回空模板，不报错。
- `GET /task/{task_id}/alarms` —— 走**数据库历史**（`clean_alarm` 表全量），DB 挂了才报错。

同一份告警在两处形态不同：`message` 是内存里的实时增量（短字段名 `metric`/`level`/`message`），`alarms` 是持久化后的历史行（全称字段 `alarm_type`/`severity`/`message`）。别把两处字段名当同一套。

**大屏清单**（跨任务）：

- `GET /task/live` —— 在线任务清单，纯内存快照。
- `GET /task/history` —— 最近 10 个可回放的历史任务，来源是磁盘上的段。

两张清单**只出参数、不出播放 URL**——前端拿参数自己拼 `/ai/video`、`/traceback/*` 的地址。

通用约定（Base URL、Gateway、错误模型、时间戳单位）见 [README](README.md)。

---

## GET /task/message/{task_id}

**用途**：前端按 `seq` 增量拉取某 task 的实时告警 + 近 10s 各指标信号，用于告警提示、指示灯等实时展示。数据全部来自**运行时内存**（`ClientQueues` 的告警环形缓冲 + 滑窗），**非 DB**，无落库延迟。适合前端 **1–2 Hz** 轮询。

**路径参数**：`task_id`（int）。
**查询参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `since_seq` | int | 否 | 0 | 游标，只返回 `seq > since_seq` 的告警；`< 0` → **400** |

### 响应 `200`

```jsonc
{
  "task_id": 123,
  "max_seq": 42,                  // 内存告警环形缓冲的当前最大 seq；下次带 since_seq=max_seq 取增量
  "signals_10s": {                // 近 10s 各指标聚合信号；键 = 已配置 detector 的 AlarmMetric 值
    "BUBBLE":       { "active": true,  "hit_count": 3, "max_conf": 0.95 },
    "BENDING":      { "active": false, "hit_count": 0, "max_conf": 0.0 },
    "TASK_TIMEOUT": { "active": false, "hit_count": 0, "max_conf": 0.0 }
    // 含全量空模板：未命中的 metric 一律 {active:false, hit_count:0, max_conf:0.0}
  },
  "alarms": [                     // seq > since_seq 的增量告警（升序，最新在后）
    {
      "seq": 40,
      "mode": "REALTIME",         // REALTIME（上升沿）| SETTLEMENT（结算）
      "metric": "BUBBLE",         // AlarmMetric 值
      "level": "high",            // low | medium | high | critical
      "message": "持续产生新气泡…疑似漏气",
      "ts": 1751800000000         // epoch 毫秒
    }
  ]
}
```

| 字段 | 类型 | 说明（含 null / 空条件） |
|------|------|------------------------|
| `task_id` | int | 回显请求的 task_id |
| `max_seq` | int | 内存告警环形缓冲的运行计数；无告警时为 `0`。**游标续拉用它，不要用 `alarms[].seq`** |
| `signals_10s` | object | 键 = 各已配置 detector 的 `AlarmMetric` 值（由 YAML 驱动，非固定枚举全集）；**始终含全量空模板**，无数据也有键 |
| `signals_10s[metric].active` | bool | 该指标在近 10s 窗口内是否活跃 |
| `signals_10s[metric].hit_count` | int | 近 10s 内命中帧数 |
| `signals_10s[metric].max_conf` | float | 近 10s 内最大置信度；无命中为 `0.0` |
| `alarms` | array | `seq > since_seq` 的增量，升序（最新在后）；无增量为 `[]` |
| `alarms[].seq` | int | 告警序号，单调递增；作续拉游标可用 `max_seq` 兜住 |
| `alarms[].mode` | string | `REALTIME`（实时上升沿）\| `SETTLEMENT`（结算补发） |
| `alarms[].metric` | string | `AlarmMetric` 值（`BUBBLE` / `BENDING` / `TASK_TIMEOUT` / `UNKNOWN`），**短名，与 `/admin` 的 `alarm_type` 不同** |
| `alarms[].level` | string | `low` \| `medium` \| `high` \| `critical`（**短名 `level`，非 `severity`/`alarm_level`**） |
| `alarms[].message` | string | 告警文案 |
| `alarms[].ts` | int | 告警时间，epoch **毫秒** |

**排序 / 增量语义**：`alarms` 从内存环形缓冲（容量 100 条）里过滤 `seq > since_seq` 得到，升序返回。`since_seq=0`（默认）返回当前缓冲中全部未淘汰的告警——**不是全历史**，历史要查下方 `/alarms`。缓冲满 100 条后最旧的会被淘汰，超出范围的旧告警只能从 DB 侧拿。

**无活跃 run**：`client_manager` 查不到该 `task_id` 时，返回空模板（`max_seq: 0`、`alarms: []`、`signals_10s` 为全零空模板），**HTTP 仍是 200，不报错**。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `400` | `since_seq < 0` | `{"detail": "since_seq must be >= 0"}` |

> 该 400 由裸 `HTTPException` 抛出，body **只有 `detail`**，没有 README 错误模型里的 `error`/`field` 字段。判分支只认 status code，别依赖 body 字段。
> 除 `since_seq < 0` 外本端点不抛异常：无活跃 run 也返回 200 空模板（见上）。

### 前端坑点

- **游标续拉**：保存返回的 `max_seq`，下次以 `since_seq=max_seq` 请求，避免重复。用 `max_seq` 而非 `alarms` 里的某个 `seq`，因为无增量时 `alarms` 为空但 `max_seq` 仍有效。
- **轮询频率**：1–2 Hz。数据是内存增量，无落库延迟。
- **`since_seq=0` 不是全历史**：只返回内存环形缓冲（100 条）里的未淘汰告警。要全部历史查 `/task/{task_id}/alarms`。
- **字段名与 `/admin` 不一致**：本端点用短名 `metric` / `level` / `message`；`/admin`（及 `alarms` 端点）用全称 `alarm_type` / `severity`(alarm_level) / `message`。别跨端点复用同一套解析。
- **`signals_10s` 始终有全量键**：未命中的 metric 也是 `{active:false, hit_count:0, max_conf:0.0}`，不会缺键；用 `active` 判活跃，别用键存在与否判断。

---

## GET /task/{task_id}/alarms

**用途**：拉取某 task 的**全部**告警历史。始终查 `clean_alarm` 表（DB），按 `create_time` **降序**（最新在前）。告警由 AlarmWorker 异步写库，故相对内存增量有**秒级延迟**；对活跃任务，DB 与内存基本一致。

**路径参数**：`task_id`（int）。
**查询参数**：无。

### 响应 `200`

```jsonc
{
  "task_id": 123,
  "total": 5,                     // alarms 长度
  "alarms": [
    {
      "alarm_id": 1001,
      "task_id": 123,
      "step_id": 10,
      "step_name": "泄漏检测",
      "alarm_type": "流程违规",     // 流程违规 | 任务超时 | mock_alarm
      "severity": "high",          // low | medium | high | critical
      "message": "...",
      "resolved": false,
      "resolved_by": null,         // 未处理时 null
      "detected_at": 1751800000000,// epoch 毫秒，可为 null
      "resolved_at": null          // epoch 毫秒，可为 null
    }
  ]
}
```

| 字段 | 类型 | 说明（含 null 条件） |
|------|------|---------------------|
| `task_id` | int | 回显请求的 task_id |
| `total` | int | `alarms` 长度 |
| `alarms[].alarm_id` | int | 业务主键 |
| `alarms[].task_id` | int | 所属 task |
| `alarms[].step_id` | int \| null | 告警所属步骤主键；表中该列可空 |
| `alarms[].step_name` | string \| null | 步骤可读别名；表中该列可空 |
| `alarms[].alarm_type` | string \| null | 全称：`流程违规` \| `任务超时` \| `mock_alarm`（**非 `/message` 的短名 `metric`**）；表中该列可空 |
| `alarms[].severity` | string \| null | `low` \| `medium` \| `high` \| `critical`（**HTTP 上报侧叫 `alarm_level`**）；表中该列可空 |
| `alarms[].message` | string \| null | 告警文案（HTTP 上报侧叫 `alarm_message`）；表中该列可空 |
| `alarms[].resolved` | bool | 是否已处理；DB 该列为 null 时**归一化为 `false`**（不会返回 null） |
| `alarms[].resolved_by` | int \| null | 处理人 id；未处理或未记录时为 **null** |
| `alarms[].detected_at` | int \| null | 检测到的时间，epoch **毫秒**；表中该列为空时为 **null** |
| `alarms[].resolved_at` | int \| null | 处理时间，epoch **毫秒**；未处理或表中该列为空时为 **null** |

**排序**：按 `create_time`（平台落库时间）**降序**，最新在前。注意排序键是 `create_time`，**不是** `detected_at`——两者可能不同，前端别假设 `alarms` 按 `detected_at` 有序。

**空结果**：无告警时返回 `{"task_id", "total": 0, "alarms": []}`，HTTP 200，不报错。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `503` | `clean_alarm` 查询失败（DB 不可用），由边界层映射自 `DatabaseError` | `{"error": "...", "retryable": true, ...}` |

### 前端坑点

- **字段名与 `/message` 不一致**：历史行用全称 `alarm_type` / `severity` / `message`（`severity` HTTP 上报侧又叫 `alarm_level`、`message` 又叫 `alarm_message`）；`/message` 增量用短名 `metric` / `level` / `message`。跨两端点别复用解析逻辑。
- **秒级延迟**：AlarmWorker 异步写库，刚触发的告警可能几秒后才出现在此端点；要"刚刚发生"的实时性用 `/task/message`。
- **`resolved` 永不为 null**：DB 该列缺失时后端补 `false`；但 `resolved_by` / `resolved_at` / `detected_at` 等仍可能为 null，需前端兜底。

---

## GET /task/live

**用途**：大屏取**在线任务清单**。数据是活跃注册表（`ClientManager`）的**纯内存快照**，零 DB、零磁盘。返回的字段即实时画面入参——本端点**只出参数、不出播放 URL**，前端自己拼 `/ai/video` 地址。

**路径参数**：无。
**查询参数**：无。

### 响应 `200`

```jsonc
{
  "total": 2,
  "tasks": [
    {
      "task_id": 101,                 // → WS /ai/video?task_id=101
      "source_ip": "10.0.0.1",        // → WS /ai/video?client_id=10.0.0.1
      "step_id": 2                    // 当前洗消阶段，仅供展示，不参与画面路由
    }
  ]
}
```

| 字段 | 类型 | 说明（含 null / 空条件） |
|------|------|------------------------|
| `total` | int | `tasks` 长度 |
| `tasks` | array | 当前活跃 run；无活跃 run 为 `[]` |
| `tasks[].task_id` | int | 运行键；作 `WS /ai/video?task_id=` 入参 |
| `tasks[].source_ip` | string | 点位标识（= `client_id`）；作 `WS /ai/video?client_id=` 入参 |
| `tasks[].step_id` | int | 当前洗消阶段，**仅供展示**，不参与画面路由 |

**排序**：按 `task_id` 升序。

**接实时画面**（两种模式详见 [ai.md](ai.md)）：

| 用 | 语义 |
|---|---|
| `?task_id=` | 锁定**这一次 run**，run 结束即止、不跟随新任务。适合针对某次任务的监看 |
| `?client_id=<source_ip>` | 跟随该**点位**的当前 run，任务来了显示、走了黑屏、换 run 自动跟。适合固定点位常亮大屏 |

### 错误

无。本端点不读 DB、不读磁盘，不抛异常；无活跃 run 时返回 `{"total": 0, "tasks": []}`，HTTP 仍是 200。

### 前端坑点

- **两种画面模式选错会体验不符**：`task_id` 锁一次 run（结束即止），`client_id` 跟点位（自动换 run）。常亮大屏要 `client_id`。
- **`step_id` 别拿去路由画面**：它只是当前阶段的展示值；实时画面按 `task_id`/`client_id` 路由，历史回放的 `step_id` 要从 `/task/history` 的 `steps[]` 里取。
- **与 `/admin-f3m8/clients` 同源**：同一份注册表快照，本接口是大屏版——去掉了队列深度等运维字段。

---

## GET /task/history

**用途**：大屏取**历史任务清单**——最近 **10** 个**已完成且能回放**的任务，按最近有画面倒序。返回的字段即历史画面入参，同样**只出参数、不出播放 URL**。

**「已完成」判定**：磁盘上有段（能播）**且** 不在活跃注册表里（跑完了）。刻意**不看** `clean_task.status`——该字段由平台业务侧写入，取值集合后端无从校验；拿它过滤等于把清单挂在未知字面量上，写错就静默变空。

**路径参数**：无。
**查询参数**：无。

### 响应 `200`

```jsonc
{
  "tasks": [
    {
      "task_id": 101,
      "source_ip": "10.0.0.1",              // DB 补；DB 不可用或表里无此任务 → null
      "latest_ms": 1700000600000,           // 最近一次有画面的时刻，也是本清单的排序键
      "steps": [                            // 时间字段只在 step 粒度给，见下方说明
        { "step_id": 1, "tracks": ["raw", "processed"], "start_ms": 1700000000000, "last_segment_ms": 1700000580000 },
        { "step_id": 2, "tracks": ["raw"],              "start_ms": 1700000590000, "last_segment_ms": 1700000600000 }
      ]
    }
  ]
}
```

| 字段 | 类型 | 说明（含 null / 空条件） |
|------|------|------------------------|
| `tasks` | array | 最多 10 条；无可回放的历史任务为 `[]`。**无 `total` 字段**（固定 10 条，无翻页语义） |
| `tasks[].task_id` | int | 任务运行键 |
| `tasks[].source_ip` | string \| null | 点位标识，由 DB `clean_task` 补；**DB 不可用或表里无此任务 → null**（清单本身照常返回，不 503） |
| `tasks[].latest_ms` | int | `max(steps[].last_segment_ms)`，epoch **毫秒**；清单排序键 + 「这是什么时候的任务」的展示值 |
| `tasks[].steps` | array | 该任务已落盘的 step，按 `step_id` 升序；两轨都无段的 step 不进清单 |
| `tasks[].steps[].step_id` | int | 回放入参：`playlist.m3u8?step_id=` |
| `tasks[].steps[].tracks` | array | 该 step **实际落盘**的轨道，按 `["raw", "processed"]` 顺序；至少一个 |
| `tasks[].steps[].start_ms` | int | 该 step 最早的段开始时间，epoch **毫秒**，双轨并集 |
| `tasks[].steps[].last_segment_ms` | int | 该 step 最晚的**段开始**时间，epoch **毫秒**，双轨并集；**不是结束时刻** |

**排序**：按 `latest_ms` 降序（同值再按 `task_id` 降序），最新在前。

**接历史画面**（详见 [traceback.md](traceback.md)）：

```
GET /traceback/task/{task_id}/playlist.m3u8?step_id={step_id}&track={track}
GET /traceback/task/{task_id}/timeline?step_id={step_id}
```

**时间字段只给到 step 粒度**，任务级不给 `start_ms`：

- 回放本身就是 step 粒度（playlist 必填 `step_id`，跨 step 聚合不支持），且**两个 step 之间可以隔任意长时间**。任务级的「最早 ~ 最晚」会跨过中间空档，既不是任务时长、也不对应任何可播放的东西，只会被误读成连续区间。
- 任务级因此只留 `latest_ms`，用途明确：清单排序键 + 「这是什么时候的任务」的展示值。

### 错误

无。**DB 不可用时仍返回 200**，`source_ip` 全为 `null`——清单的存在性判定来自磁盘，DB 只补点位显示，不因此 503（与 `/traceback` timeline 的降级策略一致）。

### 前端坑点

- **`track` 必须从 `steps[].tracks` 里挑**。playlist 的 `track` 默认 `processed`，而只落了 raw 的 step 照默认打过去就是 **404**。
- **`start_ms` / `last_segment_ms` 是双轨并集**，与 timeline 的 `start_ms`/`end_ms` 同口径——两轨段边界不一定对齐，某一轨的实际范围可能比这里窄（实测有过 20+ 秒的差）。它表达的是「这个 step 有画面的时间跨度」，别拿它跟单轨播放进度做逐帧对齐。
- **`last_segment_ms` 是最后一段的起点**，比 step 真正结束早一个段长（`last_segment_ms + 该段 EXTINF = timeline 的 end_ms`）。要精确时长用 timeline 的 `duration_ms`。
- **没有 `total`、没有分页**：固定最多 10 条。要带过滤/分页的完整任务列表用 [`GET /lab-f3m8/tasks`](lab.md)（送标页口径，只枚举 raw 轨）。
- **清单边缘可能短暂不一致**：粗筛（目录 mtime）与深扫之间任务可能刚起/刚停，清单可能短暂含一个刚起的 run 或漏一个刚停的——下一轮轮询自愈，别据此报错。

---

## 附：静默失败的几种情况

以下情况后端**不报错**，只是没有数据，排查时容易误判为 bug：

| 现象 | 后端实际状态 |
|------|------------|
| `/message` 一直返回空模板（`max_seq:0`、`alarms:[]`） | 该 `task_id` 无活跃 run（`client_manager` 查不到），后端静默返回空模板，非错误 |
| `/message` 的 `signals_10s` 全零但确有告警 | 近 10s 窗口内该指标无命中帧，或 detector 未配置该 metric（键仍在，值为空模板） |
| `/message` 用 `since_seq=0` 拿不到几分钟前的告警 | 内存环形缓冲仅 100 条，旧告警已被淘汰；历史查 `/task/{task_id}/alarms` |
| `/alarms` 返回 `total:0` 但内存里刚有告警 | AlarmWorker 尚未落库（秒级延迟），DB 里还没这条 |
| `/live` 返回 `total:0` 但任务确实在跑 | 该 run 尚未注册进 `ClientManager`（刚起）或已注销（刚停）；下一轮轮询即可见 |
| `/history` 里没有刚跑完的任务 | 该任务磁盘上无段（起流即失败）→ 不进清单；或刚停不久，粗筛/深扫窗口错开，下一轮自愈 |
| `/history` 的 `source_ip` 全是 null | DB 不可用或 `clean_task` 无此任务，清单降级返回（存在性判定来自磁盘，不 503） |
| 按 `/history` 的 `step_id` 拉 playlist 得 404 | `track` 用了默认 `processed`，但该 step 只落了 raw；`track` 必须从 `steps[].tracks` 里挑 |
