# `/api` — 统一任务 API

启动 / 终止一次 run 的唯一对外入口（桥接 `RunController`）。通用约定（错误模型、双模标识）见 [README](README.md)。

---

## POST /api/start

启动任务并拉流（合并"加载任务 + 起流"）。同步执行（内部经线程池），返回即已起流；推理在后台异步进行。

**请求体** `StartRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | int | 是 | 业务主键（= 运行键） |
| `rtsp_url` | str | 是 | RTSP 拉流地址 |

> 历史字段 `fps` 已弃用——后端从不使用（解码帧率取自 stream config，抽帧率取自 client config）。老前端继续带 `fps` 无害（Pydantic 默认忽略多余字段），新前端只需 `{ task_id, rtsp_url }`。

后端据 `task_id` 查 `clean_task` 表取 `source_ip`、`current_step`（转 int `step_id`）。

**幂等**：同 task 且 `step_id` 与 `rtsp_url` 均未变 → 幂等返回；任一变化 → 停旧 run 全量重建。

**200**：

```jsonc
{
  "status": "success",
  "task_id": 123,
  "rtsp_url": "rtsp://...",
  "message": "Task 123 started"          // 或 "Task 123 already running (idempotent)"
}
```

**错误**：`400`（`task_id`/`rtsp_url` 缺失，或该任务 DB 中 `source_ip` 为空）、`404`（task 不存在）、`503`（DB 查询失败，retryable）、`500`（workflow 启动失败）。响应体见 [错误模型](README.md#错误模型)。

---

## POST /api/terminate

终止任务并完整清理（decoder + 推理 + registry + HLS 残段）。**尽力而为，永不抛异常**——总返回 200。

双模标识（至少一个；`task_id` 优先）：

| 位置 | 参数 | 类型 | 说明 |
|------|------|------|------|
| body `TerminateRequest` | `task_id` | int | 新，首选，O(1) 直取，与 `/api/start` body 对称 |
| query | `client_id` | str | 旧，= `source_ip`，扫描匹配首个，兼容期保留 |

**200 — 命中活跃 run**（各字段为清理阶段结果）：

```jsonc
{
  "status": "success",                    // 有子步失败时为 "partial_success"
  "task_id": 123,
  "client_id": "192.168.1.100",           // 诊断字段，语义 = source_ip
  "reason": "API termination request",
  "decoder_stopped": true,
  "data_flushed": true,
  "client_cleaned": true,
  "errors": []                            // 非空则列出失败子步，如 "decoder: ...", "flush: ..."
}
```

**200 — 无活跃 run（no-op）**：`{"status": "success", "task_id": 123, "message": "no active run"}`（或以 `client_id` 键）。视作正常（任务已停）。

**错误**：`400`（`task_id` 与 `client_id` 都没给）。
