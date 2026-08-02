# `/api` — 统一任务 API

启动 / 终止一次 run 的唯一对外入口，桥接内部 `RunController`。数据不落 DB 展示层，操作的是**内存中的运行态**（decoder + 推理 workflow + client registry + HLS）。通用约定（Base URL、Gateway、错误模型、双模标识、时间戳单位）见 [README](README.md)。

```
  POST /api/start ──→ 起一次 run（task_id 为运行键）
  POST /api/terminate ──→ 停同一 run（task_id 首选，或旧 client_id）
```

---

## POST /api/start

前端用它**启动一路任务并拉流**：一次调用完成"从 DB 加载任务 + 起解码 + 起推理 workflow"。请求同步返回（内部经线程池执行到起流完成），返回即已起流；推理在后台异步进行。同一 task 重复调用是**幂等**的（见下）。

**请求体** `StartRequest`：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `task_id` | int | 是 | — | 业务主键，同时是**运行键**。后端据此查 `clean_task` 表取 `source_ip`、`current_step` |
| `rtsp_url` | str | 是 | — | RTSP 拉流地址。任意字符串，后端不校验格式，直接交 decoder |

> 历史字段 `fps` 已弃用——后端从不使用（解码帧率取自 stream config，抽帧率取自 client config）。老前端继续带 `fps` 无害（Pydantic 默认忽略多余字段），新前端只发 `{ task_id, rtsp_url }` 即可。

**幂等 / 重启语义**：后端把 DB 的 `current_step`（数字串）转成 int `step_id`。同 `task_id` 且 `step_id` 与 `rtsp_url` **均未变** → 幂等返回（不重建）；任一变化 → 先停旧 run 再全量重建。

### 响应 `200`

```jsonc
{
  "status": "success",
  "task_id": 123,
  "rtsp_url": "rtsp://...",
  "message": "Task 123 started"        // 幂等命中时为 "Task 123 already running (idempotent)"
}
```

| 字段 | 类型 | 说明（含 null 条件） |
|------|------|---------------------|
| `status` | string | 恒为 `"success"`（失败走异常 → 非 200，不会返回其他 status 值） |
| `task_id` | int | 回显请求的 `task_id` |
| `rtsp_url` | string | 回显请求的 `rtsp_url` |
| `message` | string | 新起 run → `"Task {id} started"`；幂等命中（step/url 均未变）→ `"Task {id} already running (idempotent)"`。仅供展示，**别据文案判分支** |

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `422` | 请求体缺 `task_id`/`rtsp_url` 或类型不符（FastAPI 自带校验） | `{"detail":[{"loc":[...],"msg":"...","type":"..."}]}`（**与下方业务错误的 `{"error",...}` 形态不同**） |
| `400` | 该任务 DB 中 `source_ip` 为空（`ValidationError`，`field="source_ip"`） | `{"error":"Validation error","detail":"...","field":"source_ip"}` |
| `404` | `task_id` 在 `clean_task` 表中不存在 | `{"error":"Resource not found","detail":"...","resource_type":"Task","resource_id":"123"}` |
| `503` | DB 查询失败（`DatabaseError`，可重试） | `{"error":"Internal error","detail":"...","retryable":true}` |
| `500` | workflow 启动失败等内部错误（`AppError`） | `{"error":"Internal error","detail":"...","retryable":false}` |

> **判分支只认 status code，别依赖 body 字段：** 参数缺失/类型错走 FastAPI 的 **422**（body 是 `detail` 数组），而 `source_ip` 为空是业务 **400**（body 是 `{"error","detail","field"}`）——两者都是"入参有问题"但 code 和 body 形态都不同。完整错误模型见 [README](README.md#错误模型)。

### 前端坑点

- **幂等靠后端自判，前端无需先查再起**：重复 `/api/start` 同参不会重建、不报错，直接拿到 `"already running (idempotent)"`。
- 改 `current_step`（DB 侧）或换 `rtsp_url` 后再调 `/api/start`，会**自动停旧起新**，前端无需先 `/api/terminate`。
- `source_ip` 不由前端传，取自 DB `clean_task.source_ip`；若该列为空会 400 而非 404——先确认任务已配好来源 IP。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| 返回 200 但画面/推理"没动" | 起流成功即返回 200，推理是**后台异步**；帧要经 `/ai/video`（WS）观察，`/api/start` 的 200 只代表"已起流"不代表"已出结果" |
| 反复调用只回 `idempotent`、感觉没生效 | step 和 url 都没变即幂等复用旧 run，这是正常去重；要重建得先改参数或先 terminate |

---

## POST /api/terminate

前端用它**终止一路任务并完整清理**（停 decoder + 停推理 + 落盘残余 + 清 registry + HLS 残段）。**尽力而为，永不抛异常——任何情况都返回 200**，部分子步失败用响应体字段表达（见下）。

**双模标识**（两者二选一，`task_id` 优先；都不给 → 400）：

| 位置 | 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|------|
| body `TerminateRequest` | `task_id` | int \| null | 否 | null | **新，首选**。O(1) 直取运行键，与 `/api/start` body 对称 |
| query | `client_id` | str \| null | 否 | null | **旧**，值 = `source_ip`。扫描活跃 run 匹配首个命中，兼容期保留 |

> 两键都可缺省（各自默认 null），但**运行时至少给一个**；给了 `task_id`（body）就走 task_id 分支，否则回落 `client_id`（query）。两者皆无 → `ValidationError` → 400。

### 响应 `200` — 命中活跃 run

各字段为清理各子步的结果（内部 `stop_run` 的返回，router 追加 `status`）：

```jsonc
{
  "status": "success",                  // 有子步失败时为 "partial_success"
  "client_id": "192.168.1.100",         // 诊断字段，语义 = 该 run 的 source_ip
  "reason": "API termination request",
  "decoder_stopped": true,
  "data_flushed": true,
  "client_cleaned": true,
  "errors": []                          // 非空则逐条列出失败子步
}
```

| 字段 | 类型 | 说明（含 null 条件） |
|------|------|---------------------|
| `status` | string | `errors` 为空 → `"success"`；`errors` 非空 → `"partial_success"`。**仍是 HTTP 200** |
| `client_id` | string \| null | 该 run 的 `source_ip`（诊断用，无论用哪种模式命中都回填此键）。若该 run 的 `source_ip` 本身为空串/未设，则为空串或 null |
| `reason` | string | 固定 `"API termination request"` |
| `decoder_stopped` | bool | decoder 是否已停 |
| `data_flushed` | bool | 结算 + HLS + feature 落盘是否完成 |
| `client_cleaned` | bool | registry 中的 CQ 是否已注销 |
| `errors` | string[] | 失败子步列表，形如 `"decoder: ..."`、`"flush: ..."`、`"client_manager: ..."`；全成功则 `[]` |

> 注意：命中 run 的成功响应体里**没有** `task_id` 字段（那是 no-op 响应才有的）；命中路径用 `client_id`（= source_ip）标识。别假设两条 200 路径 body 同构。

### 响应 `200` — 无活跃 run（no-op）

查不到 run（已停 / 从未起）时**不报错**，返回一个精简 no-op 体。**其身份键随输入模式不同**：

- 走 body `task_id`：`{"status": "success", "task_id": 123, "message": "no active run"}`
- 走 query `client_id`：`{"status": "success", "client_id": "192.168.1.100", "message": "no active run"}`

即：`task_id` 输入回 `task_id` 键，`client_id` 输入回 `client_id` 键——**前端别硬取某个键**，按自己传入的模式读回。视作正常（任务已停即达成目的）。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `400` | body `task_id` 与 query `client_id` **都没给**（`ValidationError`，`field="task_id"`） | `{"error":"Validation error","detail":"...","field":"task_id"}` |

> 除"两键都缺"外，本端点**不产生其他错误码**：找不到 run 是 200 no-op（非 404），清理子步失败是 200 `partial_success`（非 500）。**只有 400 一种真错误。**

### 前端坑点

- **判成败只看 `status` 字段（`success` vs `partial_success`），别看 HTTP code**——清理有失败也是 200。要重试就看 `errors` 里具体哪步炸了。
- **命中体 vs no-op 体结构不同**：命中体有 `decoder_stopped/data_flushed/client_cleaned/errors`、无 `task_id`；no-op 体只有 `status`/身份键/`message`。用 `"message" == "no active run"` 或 `errors in resp` 区分，别盲取字段。
- `client_id`（wire 键）语义就是 `source_ip`；`/api/start` 不吃这个键，别拿来当启动参数。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| 调 terminate 拿到 200 但"感觉没停" | 若是 no-op 体（`message":"no active run"`），说明该键**根本没匹配到活跃 run**（键传错 / run 早已停）——不是停失败，是压根没这路 run |
| 拿到 200 `success` 但残留未清 | 看 `partial_success` + `errors`：某子步（decoder/flush/client_manager）失败被吞进 body，主流程不抛，需据 `errors` 排查 |
