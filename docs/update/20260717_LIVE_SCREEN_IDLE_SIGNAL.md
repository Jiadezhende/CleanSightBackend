# 大屏按 source_ip 常连：`/ai/video` idle 控制帧 + source_ip「按点位跟随」正名（tie-break 最晚启动）

> **变更状态**：生效中（2026-07-17）
> **知识库**：已沉淀 → [kb/SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)(2026-07-21)
>
> 代码真源：[`websocket_video_endpoint`](../../app/routers/ai.py)、[`find_by_source_ip`](../../app/services/client/manager.py)
> 承接：`task_id` 于 [20260704_RUNKEY_TASKID_LANDING](20260704_RUNKEY_TASKID_LANDING.md) 成为 run 身份键；本次把 `?client_id=<source_ip>` 从「遗留垫片」正名为与之并列的**按点位实时跟随**请求模式（两者是正交查询轴，不冲突）。

## 概述

- **改了什么**：给 `WS /ai/video` 增加一条 edge-triggered 的 `{"type":"idle"}` 控制帧——仅在「曾推帧 → 当前无 run」跳变时发**一次**，供前端清屏黑屏。
- **为什么改**：大屏需按固定 `source_ip` 常开一条 WS 展示画面，要求「无任务黑屏、不耗费流量，有任务才显示」。系统本就是按任务 on-demand（无任务时无拉流/无解码/无 HLS，天然零流量），但 WS 帧流在任务结束后只是**停止推帧**，浏览器 `<img>` 会**冻结在最后一帧**而非变黑；且从帧流本身无法区分「任务结束」与「任务中卡顿」（两者都表现为没有新帧）。故由后端显式给出结束信号。
- **影响面**：[app/routers/ai.py](../../app/routers/ai.py) 的 WS 数据面 + [`find_by_source_ip`](../../app/services/client/manager.py) 的解析语义；无新增端点、无 schema 变化。`?task_id=` 模式同样受益（run 结束即发 idle）。

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

## 两种请求模式与 source_ip 正名

`/ai/video`（及 `/terminate`）支持两条**正交查询轴**，非新旧之分：

| 参数 | 语义 | 绑定对象 | 生命周期 | 场景 |
|------|------|---------|---------|------|
| `?task_id=X` | 跟**这一次 run** 到底 | 某个不可变运行实例 | run 结束即止，不跟随新任务 | 溯源、针对某次任务监看、告警联动 |
| `?client_id=<source_ip>` | 这个**点位**现在有什么就显示什么 | 物理摄像头 / 位置 | 自动跟随该 IP 换 run，来了显示、走了黑屏 | 大屏墙、固定点位常亮 |

- **正名**：把 `client_id` 从代码里的「旧参 / 边界垫片」注释改为一等的「按点位跟随」模式（[ai.py](../../app/routers/ai.py) docstring + [`find_by_source_ip`](../../app/services/client/manager.py) docstring），避免后人当遗留死代码清理掉——它现在承载大屏。
- **tie-break = 最晚启动**：`find_by_source_ip` 由「取首个」改为命中多个时取 `task_started_at`（[queues.py 构造时 `time.time()`](../../app/services/client/queues.py) 打戳，重启建新 CQ 即刷新）最大者——新 run 顶掉旧 run 的展示。仍是 O(N) 单遍扫描，N=并发 run 数（个位数），零额外成本。
- **不变式**：正确性依赖「同 source_ip 至多一个 live run」；多 run 并发时 tie-break 保证确定性地取最新。**WS 每轮重解析是「跟随」机制本身**，不可缓存成取一次（否则锁死旧 run、不跟随换 run/结束）。

## 已知边界（刻意不处理）

- 任务**重启**（stop→start）中间若出现短暂 `cq is None` 窗口，会闪一次 idle→黑→恢复，属正确反映。按 YAGNI 暂不加去抖；如刺眼再补几百毫秒 debounce。
- 同点位并发**多个** live run 时，展示取最晚启动者；若将来需要「指定某个展示用 step」而非纯按启动时间，再引入显式选择维度（当前无需求）。

## 验证

| 项 | 结果 |
|----|------|
| `python -m py_compile app/routers/ai.py app/services/client/manager.py` | OK，编译通过 |
