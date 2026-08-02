# `/ai` — 实时推理画面

订阅某一路 run 的**渲染后画面**（叠加了检测框的 JPEG 帧），供前端做 live view / 大屏常亮。
数据来自内存（各 run 的渲染队列），**实时**、不落库、不可回放。通用约定见 [README](README.md)。

只有一个端点：`WS /ai/video`。

---

## WS /ai/video

前端用它把某一路 run 的实时画面渲染进 `<img>`：服务器**单向推**文本帧，每帧是一张 JPEG 的 data-URL，直接塞 `img.src` 即可。客户端**无需发任何消息**（服务端会读取入向帧，仅用于感知断开）。

**查询参数**（`task_id` / `client_id` **二选一**，`task_id` 优先）：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | int | 二选一 | 锁定**某一次具体 run**（不可变运行键）。该 run 结束即画面停、不跟随新任务。适合溯源 / 针对某次任务监看。 |
| `client_id` | str | 二选一 | = `source_ip`。每轮重解析该**点位**的当前 live run（命中多个取最晚启动者）。任务来了显示、走了黑屏、换 run 自动跟随。适合大屏 / 固定点位常亮。 |

- 两者**都给**：`task_id` 优先，`client_id` 被忽略（代码先判 `task_id`，见 ai.py:65）。
- 两者**都不给**：立即以关闭码 **1008** 拒绝连接（`accept()` 之前就关，见 ai.py:76-78）。
- `task_id` **非整数**（如 `?task_id=abc`）：以关闭码 **1008** 拒绝（`int()` 抛 `ValueError`，见 ai.py:65-69）。注意此判定先于 `client_id`——`?task_id=abc&client_id=1.2.3.4` 也会 1008，不会回落到 client_id。

### 消息协议

连接建立后，服务器持续发**文本帧**（非二进制），两类：

**① 图像帧** —— JPEG 的 data-URL：

```
data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ...
```

编码链：domain `Frame` → `cv2.imencode(".jpg", ...)` → base64 → 前缀 `data:image/jpeg;base64,`（ai.py:149-152）。可直接作 `<img src>`。

**② 控制帧** —— 仅一种：

```json
{"type": "idle"}
```

前端**按前缀分流**：`data:` 开头 → 当图渲染；否则按 JSON 控制帧解析（参考实现见 admin/index.html 的 `videoWs.onmessage`）。

**`idle` 何时发（edge-triggered，务必理解）**：

- 触发条件：**当前 run 消失**（`task_id` 模式下该 run 结束；`client_id` 模式下该点位无 live run），**且之前曾推过帧**——即从"推帧态 → 无 run"的**跳变**沿发**一次**（ai.py:117-123）。
- **不去抖、不重复发**：持续无 run 期间保持静默（近零流量），不会每轮重发 idle（ai.py:118 的 `streaming` 标志兜住）。
- **发后不关连接**：连接保持存活，继续每轮解析；一旦新 run 出现（尤其 `client_id` 模式自动跟随到新 run）就恢复推图像帧，**无需前端重连**。
- **首次连上若从来没有 run**：**不发 idle**（从未进入推帧态，无跳变沿）——前端此时只会看到"连上但一直无消息"，靠占位图兜住。
- **任务中卡顿**（run 仍在、但暂无新渲染帧）：**不发 idle**，服务端保持最后一帧不动（前端画面定格，非黑屏）。这与"任务结束"是两种不同现象，见下方静默失败表。

前端收到 idle 的正确处理：清空画面回占位（"等待画面"），**不要断开、不要重连**（参考 admin/index.html 的 `videoWs.onmessage` idle 分支：把 `liveFrame` 置空、`liveIdle=true`）。

### 帧率与去重

- **事件驱动"新帧即推"**：不是定时轮询目标帧率，而是有新渲染帧就发。推速跟随源渲染流（≈ `inference_fps`，随部署而定），低运动 / 低推理帧率下每秒可能仅 1–2 帧，属正常。
- **按 `frame.timestamp` 去重**：`current_timestamp <= last_sent_timestamp` 的帧跳过、不重复编码发送（ai.py:134-137）。所以**同一时刻帧只会到达一次**。
- **30fps 是传输带宽上限，不是目标帧率**：常量 `_WS_MAX_SEND_FPS=30`（ai.py:19），只是兜住突发的传输层节流（发送间隔 ≥ 1/30 s，ai.py:141-145），源率下极少触发。别把它当作"应该收到 30fps"。
- **本端点只下发 JPEG data-URL，不带每帧 timestamp**（帧 ts 仅服务端用于去重，ai.py:134-137，不下发）。因此前端**无法在本通道做精确帧率估算或按帧时间轴对齐**——能测的只有"每秒到达帧数"这一粗略吞吐，且它受网络抖动 / 传输节流 / 突发影响，不等于真实推理帧率。
- **别假设固定帧率**：不要把 30fps 上限当成应收帧率，也不要用收包数反推推理帧率。参考实现 admin/index.html 用"每秒收包计数"（`fpsFrameCount`，每 1s 归零一次）显示的就是这个粗略吞吐值，仅供展示。

### 关闭 / 错误

| 关闭码 / 情形 | 触发条件 |
|------|---------|
| **1008**（Policy Violation） | `task_id` 与 `client_id` 都不给；或 `task_id` 非整数。**在 `accept()` 之前关**，前端 `onopen` 不会触发。 |
| 正常关闭 | 客户端主动断开；网络错误（`ConnectionReset` / `BrokenPipe`）；发送异常——服务端清理并关连接（ai.py:170-185）。 |
| 服务端关停 | uvicorn 优雅关闭（~6s 超时后强制取消任务），服务端主动关连接（ai.py:187-190）。 |

前端应对：`onclose` 里做重连退避即可；1008 属参数错误，重连前先修参数（别死循环重连）。

### 前端坑点

- **二选一、别都传**：都传时 `client_id` 被静默忽略（`task_id` 优先），不会报错。
- **`task_id` 语义 = 钉死某次 run**：run 一结束画面就停（`task_id` 模式下之后是持续静默，且因曾推过帧会先收到一次 idle）；要"点位常亮、自动跟随下一次任务"用 `client_id`。
- **`client_id` 值是 `source_ip`**，不是别处的 client 名。参考实现在 `startLive()` 里从 `liveClientInfo.source_ip` 取值（admin/index.html）；某任务 `source_ip` 为 null 时无法连流。
- **收到 idle 别重连**：它是"任务结束/无 run"的正常信号，连接仍活，新任务会自动恢复推帧（`client_id` 模式）。
- **画面定格 ≠ 断线**：run 卡顿时服务端保持最后一帧、不发任何帧也不发 idle；前端若需"卡顿检测"，自己用"最近一次收帧时间"判超时，别指望服务端通知。
- **无内置心跳**：本端点不发 ping/idle 心跳（idle 只在 run 消失跳变时发一次）。长时间无 run 会长时间静默，靠 TCP/WS 层保活或前端超时兜底。

---

## 附：静默失败的几种情况

以下情况服务端**不报错、不关连接**，只是没有图像帧，排查时最易误判为 bug：

| 现象 | 服务端实际状态 |
|------|------------|
| WS 连上（`onopen` 成功）但**一直无消息**、也无 idle | 无匹配 run：`task_id` 查无此 run，或 `client_id` 该点位当前无 live run，且**本连接从未推过帧**（无 idle 跳变沿）。属正常静默，非断线。用 `/admin` 的实时监控 / client 列表确认该 run 是否真在跑。 |
| 收到一次 `{"type":"idle"}` 后**转黑屏并静默** | run 刚结束（曾推帧 → 无 run 跳变），已发 idle 一次。`client_id` 模式下等下一次任务会自动恢复；`task_id` 模式下该 run 不会再回来。 |
| 画面**定格在最后一帧**、无 idle、无新帧 | run 仍在但暂无新渲染帧（任务刚起 / 上游卡顿）。服务端保持最后一帧、按设计不发 idle。 |
| `onclose` 立刻触发、`onopen` 从未触发（码 1008） | 参数错误：`task_id`/`client_id` 都缺，或 `task_id` 非整数。在 `accept()` 前就被关。 |
| 帧率明显低于预期（每秒 1–2 帧） | 正常：事件驱动、按 ts 去重，推速跟随源渲染率（≈ `inference_fps`）。30fps 只是上限非目标。 |

> 仓库内消费本接口的参考实现：[app/static/admin/index.html](../../app/static/admin/index.html) 的 `startLive`（约 528 行起），含 data-URL 渲染、idle 清屏、按前缀分流。
