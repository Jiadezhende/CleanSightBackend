# `/task` — 前端消息与告警历史

单个清洗任务（运行键 `task_id`）的两个只读 GET：

- `GET /task/message/{task_id}` —— 走**运行时内存增量**，前端轮询用；无活跃 run 也返回空模板，不报错。
- `GET /task/{task_id}/alarms` —— 走**数据库历史**（`clean_alarm` 表全量），DB 挂了才报错。

同一份告警在两处形态不同：`message` 是内存里的实时增量（短字段名 `metric`/`level`/`message`），`alarms` 是持久化后的历史行（全称字段 `alarm_type`/`severity`/`message`）。别把两处字段名当同一套。通用约定（Base URL、Gateway、错误模型、时间戳单位）见 [README](README.md)。

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

## 附：静默失败的几种情况

以下情况后端**不报错**，只是没有数据，排查时容易误判为 bug：

| 现象 | 后端实际状态 |
|------|------------|
| `/message` 一直返回空模板（`max_seq:0`、`alarms:[]`） | 该 `task_id` 无活跃 run（`client_manager` 查不到），后端静默返回空模板，非错误 |
| `/message` 的 `signals_10s` 全零但确有告警 | 近 10s 窗口内该指标无命中帧，或 detector 未配置该 metric（键仍在，值为空模板） |
| `/message` 用 `since_seq=0` 拿不到几分钟前的告警 | 内存环形缓冲仅 100 条，旧告警已被淘汰；历史查 `/task/{task_id}/alarms` |
| `/alarms` 返回 `total:0` 但内存里刚有告警 | AlarmWorker 尚未落库（秒级延迟），DB 里还没这条 |
