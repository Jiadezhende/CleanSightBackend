# `/admin-f3m8` — 运维 Admin

运维面板 API：聚合仪表盘、活跃 run 列表、内存告警、Prometheus 指标结构化、延迟探针。
数据全部来自**进程内存**（`client_manager` 快照 + Prometheus REGISTRY），非 DB，是**实时**状态，进程重启即清零。
均**无鉴权**、正常路径**永远返回 200**。前缀 `/admin-f3m8` 含混淆串（防自动扫描器命中），其余全局约定见 [README](README.md)。

静态运维 UI（SPA）：`GET /admin-f3m8/ui`（HTML 页面，非本文档描述的 JSON 端点）。

> **告警字段名用全称**（`alarm_type` / `alarm_level` / `alarm_message`），与 `/task/message` 的短名（`type` / `level` / `message`）不同，前端两处别混用。
> **时间戳单位不一致**：本组 `overview.timestamp` 与 `alarms[].timestamp` 是 epoch **秒**；`ping.server_time_ms` 是 epoch **毫秒**；而告警历史（`/task`、`/traceback`）用毫秒。对接时逐字段核对单位。

---

## GET /admin-f3m8/overview

聚合仪表盘：当前活跃 run（任务）数、各 run 队列深度、全局待处理帧总数。前端用它做首屏概览，**建议 ~3s 轮询**。

**请求参数**：无。

### 响应 `200`

```jsonc
{
  "timestamp": 1751800000,            // epoch 秒（注意：不是毫秒，与告警/追溯的毫秒不同）
  "active_clients": 2,                // = clients 数组长度
  "total_queued_frames": 137,         // 所有 run 的 ca_ready+ca_raw+ca_processed 之和
  "clients": [
    {
      "client_id": 123,               // = task_id（admin 页 wire 旧名，值即 task_id）
      "task_id": 123,
      "source_ip": "192.168.1.100",   // 前端据此连 /ai/video WS
      "step_id": 10,
      "queue_depths": {
        "ca_raw": 30,
        "ca_ready": 5,
        "ca_processed": 12,
        "has_rendered": true          // bool，非计数；不计入 total_queued_frames
      }
    }
  ]
}
```

| 字段 | 类型 | 说明（含缺省/空条件） |
|------|------|---------------------|
| `timestamp` | int | epoch **秒**（`int(time.time())`） |
| `active_clients` | int | 活跃 run 数 = `clients` 长度 |
| `total_queued_frames` | int | 所有 run 的 `ca_ready + ca_raw + ca_processed` 之和；**不含** `has_rendered` |
| `clients` | array | 活跃 run 列表；**无活跃 run 时为 `[]`**（不报错） |
| `clients[].client_id` | int | 注册表键，值 = `task_id`（`client_id` 为旧 wire 名） |
| `clients[].task_id` | int | 运行键 |
| `clients[].source_ip` | string | 源流 IP，前端据此连 `/ai/video` |
| `clients[].step_id` | int | 当前工步 |
| `clients[].queue_depths.ca_raw` | int | 原始帧队列深度 |
| `clients[].queue_depths.ca_ready` | int | 待推理队列深度 |
| `clients[].queue_depths.ca_processed` | int | 已处理队列深度 |
| `clients[].queue_depths.has_rendered` | bool | 是否已有渲染帧（**bool，非计数**） |

### 错误

无。永远返回 200；无活跃 run 时 `active_clients=0`、`clients=[]`。

### 前端坑点

- `timestamp` 是 epoch **秒**，`alarms[].timestamp` 也是秒，但 `/task`、`/traceback` 的时间戳是毫秒——跨端点显示时间时逐个核对，别统一乘 1000。
- `queue_depths.has_rendered` 是 bool，遍历求和时要跳过，否则 `total` 被污染（后端已只累加 3 个计数键）。
- `client_id` 与 `task_id` 恒等，任取其一即可。

---

## GET /admin-f3m8/clients

活跃 run（任务）**轻量列表**，供前端下拉框选择。元素结构与 `overview.clients[]` 完全一致。

**请求参数**：无。

### 响应 `200`

**直接返回数组**（不是 `{"clients": [...]}` 包裹，与 `overview` 的包裹形态不同）：

```jsonc
[
  {
    "client_id": 123,
    "task_id": 123,
    "source_ip": "192.168.1.100",
    "step_id": 10,
    "queue_depths": { "ca_raw": 30, "ca_ready": 5, "ca_processed": 12, "has_rendered": true }
  }
]
```

字段语义同 `overview.clients[]`（见上）。无活跃 run 时返回 `[]`。

### 错误

无。永远返回 200。

### 前端坑点

- 顶层是**裸数组**，不是对象。`overview` 把同样的列表放在 `.clients` 键下——两处解析方式不同，别照抄。

---

## GET /admin-f3m8/clients/{client_id}/alarms

读取某个 run（`task_id`）**内存告警环形缓冲**里最近 n 条告警（**不走 DB**，进程重启清零）。前端用它在 admin 页展示某 run 的近期告警。

**路径参数**：`client_id`（int，= `task_id`）。
**查询参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `n` | int | 否 | 20 | 返回条数，范围 **1–100**；越界（`<1` 或 `>100`）由 FastAPI 校验返回 **422** |

### 响应 `200`（找到 run）

```jsonc
{
  "client_id": 123,
  "alarms": [                         // 最新在数组末尾（尾部为最近）
    {
      "seq": 40,                      // 该 run 内自增序号，落缓冲时补全
      "alarm_type": "流程违规",        // 全称字段；枚举见 README
      "alarm_level": "high",          // low / medium / high / critical
      "alarm_message": "...",
      "mode": "REALTIME",             // REALTIME / SETTLEMENT
      "metric": "BUBBLE",             // BUBBLE / BENDING / TASK_TIMEOUT / UNKNOWN
      "stage": "LEAK",                // 工步别名，temporal actor 产出时烧入；未填时为 ""
      "timestamp": 1751800000         // epoch 秒（int(a.timestamp)，注意不是毫秒）
    }
  ]
}
```

### 响应 `200`（未找到 run）

`client_id` 不在活跃注册表时**仍返回 200**，用 `error` 字段表达：

```jsonc
{
  "client_id": 999,
  "alarms": [],
  "error": "client_not_found"
}
```

| 字段 | 类型 | 说明（含空/缺省条件） |
|------|------|---------------------|
| `client_id` | int | 回显请求路径值 |
| `alarms` | array | 告警列表，**最新在末尾**；run 存在但无告警时为 `[]` |
| `alarms[].seq` | int | run 内自增序号 |
| `alarms[].alarm_type` | string | 全称，枚举 `流程违规`/`任务超时`/`mock_alarm` |
| `alarms[].alarm_level` | string | `low`/`medium`/`high`/`critical` |
| `alarms[].alarm_message` | string | 告警文案 |
| `alarms[].mode` | string | `REALTIME`/`SETTLEMENT` |
| `alarms[].metric` | string | `BUBBLE`/`BENDING`/`TASK_TIMEOUT`/`UNKNOWN` |
| `alarms[].stage` | string | 工步别名；未产出别名时为 **`""`**（非 null） |
| `alarms[].timestamp` | int | epoch **秒** |
| `error` | string | 仅未找到 run 时出现，值 `"client_not_found"`；**找到时该键不出现** |

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `422` | `n < 1` 或 `n > 100` | FastAPI 校验错误 `{"detail":[{...}]}` |

> run 未找到**不是 404**：返回 200 + `error="client_not_found"` + `alarms=[]`。判"run 是否存在"看 `error` 键是否存在，或 `alarms` 是否与之配套——**别指望 404**。

### 前端坑点

- 告警字段用**全称**（`alarm_type`/`alarm_level`/`alarm_message`），`/task/message` 的同类字段是**短名**（`type`/`level`/`message`）；两端点数据不要共用同一套解析。
- `timestamp` 是 epoch **秒**，与 `/task`、`/traceback` 的毫秒不同——渲染前统一单位。
- `stage` 空值是空串 `""` 不是 null。
- 数组尾部是最近告警（`items[-n:]` 语义），倒序展示需自行 reverse。
- 区分"run 无告警"与"run 不存在"：前者 `alarms=[]` 且无 `error`；后者 `alarms=[]` 且 `error="client_not_found"`。

---

## GET /admin-f3m8/metrics/json

把 Prometheus 5 个核心指标从 REGISTRY 解析为**结构化 JSON**（免前端自己解析 `/metrics` 文本）。前端用它做指标看板，**建议 ~5s 刷新**。

**请求参数**：无。

### 响应 `200`

**每个指标族只有在其被记录过（存在于 REGISTRY）时才出现对应 key；从未记录过的指标——整个 key 缺省，不是 0。** 前端判"暂无数据"应检查 **key 是否存在**，不能默认 key 一定在。

```jsonc
{
  // 1) 推理延迟 Histogram，按模型名分组
  "infer_latency_ms": {
    "yolov8n": { "p50": 12.5, "p95": 30.1, "p99": 45.0, "total_count": 1000 }
  },
  // 2) 推理失败 Counter
  "infer_failure_total": {
    "total": 3,
    "by_type": { "cuda_oom": 2, "timeout": 1 }
  },
  // 3) 帧丢弃 Counter
  "frame_drop_total": {
    "total": 58,
    "by_reason": { "queue_full": 50, "stale": 8 }
  },
  // 4) GPU OOM Counter —— 直接是 int，不是对象
  "gpu_oom_total": 2,
  // 5) 重试 Counter
  "retry_total": {
    "total": 7,
    "by_operation": { "rtsp_connect": 5, "db_write": 2 }
  }
}
```

| 字段 | 类型 | 出现条件 / 说明 |
|------|------|---------------|
| `infer_latency_ms` | object | 仅 `infer_latency_ms` 直方图有采样时出现；键为模型名 |
| `infer_latency_ms.<model>.p50/p95/p99` | float | 从桶插值估算的分位数（ms）；无有效样本时为 `0.0` |
| `infer_latency_ms.<model>.total_count` | int | 该模型观测总次数 |
| `infer_failure_total` | object | 仅 `infer_failure` Counter 存在时出现 |
| `infer_failure_total.total` | int | 失败总数 |
| `infer_failure_total.by_type` | object | 按 `error_type` 分组计数 |
| `frame_drop_total` | object | 仅 `frame_drop` Counter 存在时出现 |
| `frame_drop_total.total` | int | 丢帧总数 |
| `frame_drop_total.by_reason` | object | 按 `reason` 分组计数 |
| `gpu_oom_total` | int | 仅 `gpu_oom` Counter 存在时出现；**值直接是 int，非对象** |
| `retry_total` | object | 仅 `retry` Counter 存在时出现 |
| `retry_total.total` | int | 重试总数 |
| `retry_total.by_operation` | object | 按 `operation` 分组计数 |

### 错误

无 HTTP 错误码。解析过程抛任何异常 → **降级返回 `{}`**（后端记 warning，前端拿到空对象）。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| 返回 `{}`（整个空对象） | 要么 5 个指标全未记录过，要么解析抛异常降级——两者前端**无法区分**，都当"暂无数据"处理即可 |
| 某个指标 key 缺失（如无 `gpu_oom_total`） | 该指标从未被记录（Counter/Histogram 未注册或无样本），**不是值为 0**——前端应据"key 存在与否"判断，别默认取值 |
| `infer_latency_ms.<model>` 的 `pXX` 全为 `0.0` | 有直方图但样本数 `total<=0` 或桶为空，分位数无法估算 |

---

## GET /admin-f3m8/ping

延迟探针：立即返回服务端时间戳，前端用 `本地接收时刻 - server_time_ms` 估算 RTT / 时钟偏差。

**请求参数**：无。

### 响应 `200`

```jsonc
{
  "server_time_ms": 1751800000000.0   // epoch 毫秒（float，time.time()*1000）
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `server_time_ms` | float | epoch **毫秒**（浮点，含小数）；注意本值单位与本组其余端点（秒）不同 |

### 错误

无。永远返回 200。

### 前端坑点

- `server_time_ms` 是**毫秒**（float），而同组的 `overview.timestamp` / `alarms[].timestamp` 是**秒**（int）——同一面板混用时极易错位。
