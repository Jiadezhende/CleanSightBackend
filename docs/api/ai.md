# `/ai` — 实时推理画面

## WebSocket /ai/video

订阅一路 run 的渲染后画面。**服务器单向推送**，客户端不需发消息（发了也会被读取以感知断开）。通用约定见 [README](README.md)。

**查询参数**（至少一个）：

| 参数 | 类型 | 说明 |
|------|------|------|
| `task_id` | int | 首选，绑定固定 run（该 run 停则画面停） |
| `client_id` | str | 旧，= `source_ip`，每轮重解析，跟随该 source_ip 的当前 run |

两者都不给 → 以关闭码 **1008** 拒绝连接。

**消息协议**：服务器每帧发一个**文本帧**（非二进制），内容为 JPEG 的 data-URL：

```
data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ...
```

- 编码：`cv2.imencode(".jpg", frame)` → base64 → 前缀 `data:image/jpeg;base64,`。可直接作 `<img src>`。
- 帧率：**事件驱动"新帧即推"**——按 `frame.timestamp` 去重（同一帧不重发），推速跟随源渲染流（≈ `inference_fps`，当前 ~15fps），低运动时可能每秒仅 1–2 帧。30fps 是**传输带宽上限**（服务端 `_WS_MAX_SEND_FPS`，突发节流用），非目标帧率，源率下极少触发。
- 无画面（run 未起 / 已停）：服务器不发帧，短暂等待后重试；连接保持。

**关闭 / 错误**：客户端断开、网络错误（`ConnectionReset`/`BrokenPipe`）、服务器关停（优雅超时 ~6s）均清理并关闭连接。
