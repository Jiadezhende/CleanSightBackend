# 前端换键迁移指引（临时）

> 临时文档，供业务前端从 `client_id` 迁移到 `task_id`。迁移完成后本文与后端 `client_id` 分支一并下线。
> 后端改动：commit `f7f39c5`（`refact/key-change`）。

## 一句话

后端已把内部运行键统一为 **`task_id`（整数）**。历史上有两个业务接口用 `?client_id=<设备IP>` 调用，
`client_id` 实际传的是 **source_ip（设备 IP）**，并不是真正的客户端 ID。现在这两个接口**同时接受
`task_id` 和 `client_id`**，请前端逐步改用 `task_id`。切完后端会删掉 `client_id` 分支。

## 影响的接口（只有两个）

| 接口 | 旧调用 | 新调用 |
|------|--------|--------|
| WS `/ai/video`（实时画面） | `?client_id=<source_ip>` | `?task_id=<task_id>` |
| POST `/api/terminate`（终止任务） | `?client_id=<source_ip>` | `?task_id=<task_id>` |

> 其余接口（`/api/start`、`/task/{task_id}/alarms`、`/task/message/{task_id}`）本来就用 `task_id`，无需改。

## task_id 从哪来

前端调 `POST /api/start` 时**自己就带着 `task_id`**（请求体 `{ task_id, rtsp_url, fps }`）。
这个 `task_id` 就是后续所有接口的唯一标识——直接复用即可，不用再去查 source_ip。

## 迁移方式（两个都是「加一个参数、换一个参数名」）

### 1. 实时画面 WS `/ai/video`

```js
// 旧
new WebSocket(`${wsBase}/ai/video?client_id=${sourceIp}`);

// 新 —— 直接用启动时的 task_id
new WebSocket(`${wsBase}/ai/video?task_id=${taskId}`);
```

行为差异（新的更符合预期）：
- **`task_id`**：锁定这一个具体任务的画面，任务不变就一直看它。
- **`client_id`（旧）**：按设备 IP 找「当前第一个匹配的 run」，同 IP 换任务时会自动跳到新任务——语义模糊，不建议再依赖。

其余不变：连上后后端持续推 `data:image/jpeg;base64,...` 文本帧；参数都不传 → 后端以 `1008` 关闭连接。

### 2. 终止任务 POST `/api/terminate`

```js
// 旧
fetch(`${apiBase}/api/terminate?client_id=${sourceIp}`, { method: 'POST' });

// 新
fetch(`${apiBase}/api/terminate?task_id=${taskId}`, { method: 'POST' });
```

响应不变：
- 成功：`{ status: "success" | "partial_success", ... }`
- 任务不在（已停/从未起）：`{ status: "success", task_id, message: "no active run" }`（幂等 no-op，可当成功处理）
- **两个参数都不传** → 后端报校验错误（HTTP 4xx）；老代码只要还带着 `client_id` 就不会触发。

## 兼容期约定

- **过渡期**：`task_id` 与 `client_id` 同时可用；**两者都传时 `task_id` 优先**。前端可任意批次切换，不用和后端约时间。
- **两个接口切完后**通知后端，届时删除 `client_id` 分支——之后再带 `client_id` 会失效。
- admin / lab 后台前端由后端同学一并处理，业务前端不用管。

## 自测清单

- [ ] `/ai/video?task_id=<task_id>` 能正常出画面
- [ ] `/api/terminate?task_id=<task_id>` 能正常停任务，返回 `status: success`
- [ ] 全站已无 `client_id=` 的调用残留（grep 一遍）→ 通知后端下线
