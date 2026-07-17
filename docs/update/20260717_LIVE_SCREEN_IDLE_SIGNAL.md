# `/ai/video` 增加 idle 控制帧：大屏按 source_ip 常连、任务结束自动黑屏

> **变更状态**：生效中（2026-07-17）
> **知识库**：待沉淀
>
> 代码真源：[`websocket_video_endpoint`](../../app/routers/ai.py)
> 承接：本次建立在 `?client_id=<source_ip>` 边界垫片（每轮 `find_by_source_ip` 解析当前 run）之上，不改其语义。

## 概述

- **改了什么**：给 `WS /ai/video` 增加一条 edge-triggered 的 `{"type":"idle"}` 控制帧——仅在「曾推帧 → 当前无 run」跳变时发**一次**，供前端清屏黑屏。
- **为什么改**：大屏需按固定 `source_ip` 常开一条 WS 展示画面，要求「无任务黑屏、不耗费流量，有任务才显示」。系统本就是按任务 on-demand（无任务时无拉流/无解码/无 HLS，天然零流量），但 WS 帧流在任务结束后只是**停止推帧**，浏览器 `<img>` 会**冻结在最后一帧**而非变黑；且从帧流本身无法区分「任务结束」与「任务中卡顿」（两者都表现为没有新帧）。故由后端显式给出结束信号。
- **影响面**：仅 [app/routers/ai.py](../../app/routers/ai.py) 的 WS 数据面；无新增端点、无 schema 变化。`?task_id=` 模式同样受益（run 结束即发 idle）。

## 协议（前后端契约）

同一条 WS 上按**文本前缀**区分两类消息：

| 消息 | 内容 | 触发 | 前端处理 |
|------|------|------|---------|
| 帧 | `data:image/jpeg;base64,...` | 有 run 且有新渲染帧 | `img.src = data` |
| idle | `{"type":"idle"}` | 从「曾推帧」跳变到「当前无 run」，发一次 | `img.src = ""` 清屏黑 |

三态行为：

| 状态 | cq | 后端 | 大屏 | 流量 |
|------|----|------|------|------|
| 一直无任务 | None（从未推过） | 静默（`streaming` 恒 False，不发 idle） | 默认黑（CSS） | 0 |
| 任务中卡顿 | 存在，无新帧 | 静默，保持 `streaming=True` | 保持最后一帧（不误黑） | 0 |
| 任务结束 | None（此前 `streaming=True`） | 发**一次** idle，置 `streaming=False` | 清屏→黑 | 一条 idle 后归 0 |

> **为什么 edge-triggered**：若无 run 期间每轮都发 idle（0.05s 一条）会变成持续流量，违背「不耗费流量」。故用 `streaming` 状态位只在跳变发一次。

## 改动详情

### `app/routers/ai.py` — 主循环拆分「无 run / 有 run 无帧 / 有帧」三支

- 新增状态位 `streaming`（初始 `False`），成功推帧后置 `True`。
- `cq is None`：若 `streaming` 为真，发一次 `{"type":"idle"}` 并置 `False`；随后 `sleep(0.05)` 静默轮询。
- `cq` 在但 `get_latest_rendered()` 为 None（任务刚起 / 卡顿）：保持最后一帧、**不**发 idle。
- 依赖不变式：`frame.timestamp` 为解码时 `time.time()` 墙钟（见 [`FFmpegDecoder`](../../app/services/stream/decoder.py)），单调递增——同 source_ip 换 run 时新帧时间戳必更高，既有去重（`current_timestamp <= last_sent_timestamp`）不会误吞新任务首帧，故无需跨 run 重置。

## 已知边界（刻意不处理）

- 任务**重启**（stop→start）中间若出现短暂 `cq is None` 窗口，会闪一次 idle→黑→恢复，属正确反映。按 YAGNI 暂不加去抖；如刺眼再补几百毫秒 debounce。
- `find_by_source_ip` 取首个匹配，`source_ip` 业务不保证唯一；大屏绑定单摄像头通常一 IP 一任务，无歧义。

## 验证

| 项 | 结果 |
|----|------|
| `python -m py_compile app/routers/ai.py` | OK，编译通过 |
