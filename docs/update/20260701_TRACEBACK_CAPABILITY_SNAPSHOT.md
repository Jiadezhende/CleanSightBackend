# 视频追溯能力现状快照（keypoints 下线后）

> **变更状态**：生效中（2026-07-01）　<!-- 描述当前系统实际提供的追溯能力，非一次性代码改动 -->
> **知识库**：待沉淀　<!-- 定期维护时沉淀进 docs/kb/SERVICE_TRACEBACK_MEDIA.md -->
>
> 相关：[TRACEBACK_API.md](../archive/old_architecture/TRACEBACK_API.md)（对外 API 详表，已同步）、[20260627_DROP_DEAD_KEYPOINTS_LANDING.md](20260627_DROP_DEAD_KEYPOINTS_LANDING.md)（keypoints 落地下线）、[docs/kb/SERVICE_TRACEBACK_MEDIA.md](../kb/SERVICE_TRACEBACK_MEDIA.md)（服务架构）。

## 概述

- **背景**：keypoints（推理关键点回溯）能力已废弃，但 [TRACEBACK_API.md](../archive/old_architecture/TRACEBACK_API.md) 长期残留其端点与字段描述，与代码实现不符。
- **动作**：核对 [app/routers/traceback.py](../../app/routers/traceback.py) 与 [app/routers/media.py](../../app/routers/media.py) 全部实现，清理文档中的 keypoints 残留，并以本文快照锁定当前**真实**追溯能力，作为前端对接与后续沉淀的唯一准绳。
- **影响面**：仅文档。运行时接口零改动。

## 当前追溯能力（以代码为准）

系统采用两层路由：`/traceback/*` 业务查询层返回带 HMAC token 的媒体 URL，`/media/*` 访问层校验 token 后流式返回文件。所有落盘按 `(task_id, step_id)` 隔离，链路不读 `source_ip`。

### 业务层 `/traceback/*`（4 个端点）

| 端点 | 方法 | 关键参数 | 返回 | 代码 |
|------|------|---------|------|------|
| `/traceback/alarm/{alarm_id}/evidence` | GET | `n_before`/`n_after`（default=-1, ge=-1, le=20，-1 取配置默认） | JSON：`alarm` + `task_id` + `step_id` + `raw_clips[]` + `processed_clips[]` | [traceback.py:160](../../app/routers/traceback.py#L160) |
| `/traceback/alarm/{alarm_id}/playlist.m3u8` | GET | `track`（default=processed, `^(raw\|processed)$`）、`n_before`/`n_after` | VOD m3u8（trigger 段 + 上下文，含 `#EXT-X-MAP`） | [traceback.py:373](../../app/routers/traceback.py#L373) |
| `/traceback/task/{task_id}/playlist.m3u8` | GET | `step_id`（**必填**）、`track`（default=processed） | VOD m3u8（该 step 整段回放） | [traceback.py:332](../../app/routers/traceback.py#L332) |
| `/traceback/task/{task_id}/timeline` | GET | `step_id`（**必填**） | JSON：`start_ms`/`end_ms`/`duration_ms`/`events[]` | [traceback.py:469](../../app/routers/traceback.py#L469) |

`evidence` 是**双轨能力唯一的并列出口**：`raw_clips` / `processed_clips` 两条列表，每段含 `url`/`filename`/`ts_us`/`ts_ms`/`is_trigger`（[`_segment_to_url`](../../app/routers/traceback.py#L61)）。注意 `*_clips[].url` 是裸 fMP4 fragment（无 init），不能直接喂 `<video>`，回放须走 `playlist.m3u8` 端点。

### 访问层 `/media/*`（2 个端点）

| 端点 | 方法 | kind | 代码 |
|------|------|------|------|
| `/media/segment/{token}` | GET | `segment`（`.mp4` 段流，`Cache-Control: private, max-age=60`） | [media.py:56](../../app/routers/media.py#L56) |
| `/media/init/{token}` | GET | `init`（`init.mp4`，`max-age=3600`，playlist 的 `#EXT-X-MAP` 自动签发） | [media.py:79](../../app/routers/media.py#L79) |

Token payload 只剩两种 kind：`{"t","s","f","k":"segment"|"init","e"}`，HMAC-SHA256 签名、两 kind 不可互换。

## 已废弃 / 不再存在

> 明确列出，避免后人误认为遗漏或试图调用。

- **`GET /media/keypoints/{token}`**：端点不存在。[media.py](../../app/routers/media.py) 全文仅 `segment` / `init` 两个路由。
- **`evidence` 响应的 `keypoints_url` / `detection` 字段**：不返回。实际返回见 [traceback.py:210](../../app/routers/traceback.py#L210)（`alarm`/`task_id`/`step_id`/`raw_clips`/`processed_clips`）。
- **`keypoints_{ts_us}.json` 落盘 / token kind `keypoints`**：均已下线（见 [20260627_DROP_DEAD_KEYPOINTS_LANDING.md](20260627_DROP_DEAD_KEYPOINTS_LANDING.md)）。
- **`client_id` 字段**：更早已移除，追溯链路不依赖 `source_ip`。

## 前端调用路径（端到端）

1. **告警双轨复核**：`GET .../evidence` 拿双轨元数据 → 各请求一条 `.../alarm/{id}/playlist.m3u8?track=raw|processed` 喂 `hls.js` → hls.js 自动经 `/media/init` + `/media/segment` 拉流；用 `(clips[triggerIdx].ts_ms - clips[0].ts_ms)/1000` seek 到触发段。
2. **单步骤完整回放 + 打点**：`GET .../task/{id}/playlist.m3u8?step_id=&track=` 播放 + `GET .../task/{id}/timeline?step_id=` 在进度条叠加告警标记。
3. **告警跳转回放**：`evidence` 顶层的 `task_id`/`step_id` 直接拼步骤回放 URL。

## 文档改动详情

`docs/TRACEBACK_API.md`（共 9 处）：更新时间戳；删除文件布局中的 `keypoints_*.json` 行、"keypoints 与 processed 一一对应"说明、路由图与 token payload 中的 `keypoints` kind、`evidence` 响应的 `keypoints_url`/`detection` 字段及其字段表说明、第 7 节 `/media/keypoints/{token}` 整节、前端示例中的 `ev.detection` 引用；并在字段表补一句 keypoints 废弃说明。

## 验证

| 项 | 结果 |
|----|------|
| 文档 keypoints/detection 残留扫描 | 仅剩 1 处刻意保留的废弃说明 |
| 端点签名逐一比对（route decorator + Query 校验） | 4 个 traceback + 2 个 media 全部与文档一致 |
| 代码改动 | 无（本次纯文档） |
