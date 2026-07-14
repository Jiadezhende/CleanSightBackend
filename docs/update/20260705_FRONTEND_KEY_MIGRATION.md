# 前端换键迁移指引（临时）

> 临时文档，供业务前端从 `client_id` 迁移到 `task_id`，并顺带清理 `start` 的死字段 `fps`。
> **本次全是加法：老调用一个都不会失效**，前端可逐接口、逐批次切换，切完通知后端下线旧分支。

## 一句话

后端把内部运行键统一为 **`task_id`（整数）**。历史上两个业务接口用 `?client_id=<设备IP>` 调用，
`client_id` 实际是 **source_ip（设备 IP）**，并非真正的客户端 ID。现在这些接口**同时接受新旧两种传法**，
新传法一律用 `task_id`。切完后端删掉 `client_id`/旧参分支。

## 影响的接口

| 接口 | 老调用（继续支持） | 新调用（首选） |
|------|--------|--------|
| WS `/ai/video`（实时画面） | `?client_id=<source_ip>` | `?task_id=<task_id>` |
| POST `/api/terminate`（终止任务） | query `?client_id=<source_ip>` | **body `{ task_id }`** |
| POST `/api/start`（启动任务） | body `{ task_id, rtsp_url, fps }` | body `{ task_id, rtsp_url }`（去掉 `fps`） |

> 其余接口（`/task/{task_id}/alarms`、`/task/message/{task_id}`）本来就用 `task_id`，无需改。

## task_id 从哪来

前端调 `POST /api/start` 时**自己就带着 `task_id`**（请求体 `{ task_id, rtsp_url }`）。
这个 `task_id` 就是后续所有接口的唯一标识——直接复用即可，不用再去查 source_ip。

## 迁移方式

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

新传法改用 **body**（和 `start` 对称，不再走 query）：

```js
// 旧
fetch(`${apiBase}/api/terminate?client_id=${sourceIp}`, { method: 'POST' });

// 新 —— body 传 task_id
fetch(`${apiBase}/api/terminate`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ task_id: taskId }),
});
```

后端取值优先级：**`body.task_id`（新）> query `?client_id=`（老兼容）**。两者皆缺 → 校验错误（HTTP 4xx）。

响应不变：
- 成功：`{ status: "success" | "partial_success", ... }`
- 任务不在（已停/从未起）：`{ status: "success", task_id, message: "no active run" }`（幂等 no-op，可当成功处理）

### 3. 启动任务 POST `/api/start`（去掉死字段 `fps`）

```js
// 旧
fetch(`${apiBase}/api/start`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ task_id: taskId, rtsp_url: rtspUrl, fps: 30 }),
});

// 新 —— 不用再传 fps
fetch(`${apiBase}/api/start`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ task_id: taskId, rtsp_url: rtspUrl }),
});
```

`fps` 后端**从来不用**（解码帧率取自 stream config，抽帧率取自 client config）。老前端继续带 `fps` **无害**——多余字段会被忽略；新前端直接不传即可。

## 兼容期约定

- **过渡期**：新旧传法同时可用；terminate 三来源按上面的优先级取值。前端可任意批次切换，不用和后端约时间。
- **三个接口都切完后**通知后端，届时删除 `client_id`/query 旧分支——之后再带旧参会失效。
- admin / lab 后台前端由后端同学一并处理，业务前端不用管。

## 自测清单

- [ ] `/ai/video?task_id=<task_id>` 能正常出画面
- [ ] `/api/terminate` body `{ task_id }` 能正常停任务，返回 `status: success`
- [ ] `/api/start` body 去掉 `fps` 后仍能正常启动
- [ ] 全站已无 `client_id=` 调用、`start` 已不再传 `fps`（grep 一遍）→ 通知后端下线
