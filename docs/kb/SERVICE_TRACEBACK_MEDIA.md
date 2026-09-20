> 更新时间：2026-09-02
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Traceback And Media Service

追溯服务负责按告警或任务步骤定位 HLS 段，并通过 token 化媒体路由返回可播放资源。

两层路由：`/traceback/*` 业务查询层返回带 HMAC token 的媒体 URL；`/media/*` 访问层校验 token 后流式返回文件。所有落盘按 `(task_id, step_id)` 隔离，链路不读 `source_ip`（`alarm` 表自带 `(task_id, step_id)` 直接定位）。

## SegmentFinder

`SegmentFinder` 基于目录约定：

```text
{base_dir}/{task_id}/{step_id}/
```

它按文件名中的 `ts_us` 列出和定位：

- raw segment
- processed segment

`find()` 根据目标 `ts_ms` 返回触发段和前后上下文。

除按 `ts_us` 定位（`find()` / `list_segments()`）外，`SegmentFinder` 是**落盘目录枚举的单一真源**（traceback 与 lab 共用，lab router 不再自行遍历目录）：

- `StepRef`（`task_id / step_id / tracks / first_ts_us / last_ts_us`）：一个 step 的双轨摘要。
- `_scan_step_dir()`（私有）：单次 `iterdir` 按轨道分组，双轨枚举只付一次目录遍历；`list_segments()` 是其薄封装，对外行为不变（含非法 track 抛 `ValueError`）。
- `list_steps(task_id)`：该 task 下已落盘 step 的 `StepRef` 列表，两轨皆空的 step 丢弃。
- `list_task_ids()`：存储根下的数字目录（跳过 `.lab_exports`）。
- `list_task_ids_by_recency()`：**廉价粗排**，排序键 = `max(step 目录 mtime)`，只 `stat` 目录不读段文件（成本 O(目录数)）。**mtime 仅用于挑深扫候选，绝不当对外时间戳**——对外时间一律取 `list_steps()` 的真实 `ts_us`。
- `parse_playlist_durations(playlist_path)`（模块级函数）：解析写入侧 playlist 得 `{段文件名: EXTINF}`。原住在 `routers/traceback.py`，因是纯文件系统解析、无 HTTP 语义而下沉——否则 lab 的 `StepExporter` 要反向 import router 才能复用。**返回值同时承担两个职责**，traceback VOD 与 lab 整段导出都依赖：`EXTINF` 是段时长唯一真值（不能用文件名 `ts_us` 差重推，会与 fragment 实际媒体时长对不上）；**键集合即「已完成 transcode+append 的段」，不在其中的是在途段必须过滤**。

> 注意 `ClipBuilder`（送标）**不走这条闸**：它只 `list_segments` + 按相邻 `ts_us` 中位差估段尾，故会吃进在途的裸 mp4v 段（见 [SERVICE_LAB.md](SERVICE_LAB.md)）。段级定位当前共三套实现（`SegmentFinder.find` bisect / `Timeline.iter` searchsorted / `ClipBuilder._select_segments` 区间重叠），根因是 `SegmentFinder` 缺区间版 API。

## MediaToken

媒体 URL 不暴露物理路径。`MediaToken` 生成 HMAC-SHA256 短 TTL token，payload 包含：

- `task_id`
- `step_id`
- `filename`
- `kind`
- `expiry`

`settings.media_token_secret` 为空时，默认 token secret 在进程内生成，重启后旧 token 失效。

token payload 只剩两种 kind：`segment` | `init`（`{"t","s","f","k","e"}`），两 kind 不可互换（`verify(kind=...)` 强校验）。

## /traceback/*（业务层，4 个端点）

| 端点 | 方法 | 关键参数 | 返回 |
|------|------|---------|------|
| `/alarm/{alarm_id}/evidence` | GET | `n_before`/`n_after`（default=-1, ge=-1, le=20，-1 取配置默认 `traceback_context_before/after`） | JSON：`alarm`+`task_id`+`step_id`+`raw_clips[]`+`processed_clips[]` |
| `/alarm/{alarm_id}/playlist.m3u8` | GET | `track`（default=processed，`^(raw\|processed)$`）、`n_before`/`n_after` | VOD m3u8（trigger 段 + 上下文，含 `#EXT-X-MAP`） |
| `/task/{task_id}/playlist.m3u8` | GET | `step_id`（**必填**）、`track`（default=processed） | VOD m3u8（该 step 整段回放；任务级跨 step 聚合本期不支持） |
| `/task/{task_id}/timeline` | GET | `step_id`（**必填**） | JSON：`start_ms`/`end_ms`/`duration_ms`/`events[]` |

两个 `playlist.m3u8` 端点以 `@router.api_route(methods=["GET","HEAD"])` 注册（非 `@router.get`）：FastAPI 的 `APIRoute` 不像 Starlette 原生 `Route` 那样给 GET 自动补 HEAD，漏注册即 405——既不合 RFC 9110，又会被网关反扫描当扫描特征累计。原生 HLS 播放栈（Safari/AVPlayer/iOS WebView）取 playlist 前自动发 HEAD 探可用性；HEAD 照常执行 handler（扫段+拼 playlist 后丢弃）给出正确 200/404，body 由传输层抑制、`Content-Length` 保留真值。三档网关策略见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。

`evidence` 是**双轨能力唯一的并列出口**：`raw_clips` / `processed_clips` 两条列表，每段含
`url`/`filename`/`ts_us`/`ts_ms`/`is_trigger`（`_segment_to_url`，traceback.py:61）。注意
`*_clips[].url` 是裸 fMP4 fragment（无 init），不能直接喂 `<video>`，回放须走 `playlist.m3u8` 端点。

## /media/*（访问层，2 个端点）

媒体路由只接受 token：

- `/media/segment/{token}`：返回 mp4 fragment，`Cache-Control: private, max-age=60`。
- `/media/init/{token}`：返回 `{track}_init.mp4`（**按轨各一份**，同轨内所有段共享），`max-age=3600`；由 playlist 的 `#EXT-X-MAP` 自动签发。

路径解析会拒绝 path traversal，并确保文件在 base_dir 内（`_resolve_media_path`，media.py:27）。

## VOD playlist 原则

VOD playlist 不直接暴露落盘 LIVE playlist，而是动态生成：

- 带 `#EXT-X-PLAYLIST-TYPE:VOD`
- 带 `#EXT-X-ENDLIST`
- 每个 segment URL 都是 token URL
- 只使用写入侧 playlist 中已有 EXTINF 的段，过滤在途段

## 已废弃 / 不再存在

避免误认为遗漏或试图调用（快照见 update/20260701_TRACEBACK_CAPABILITY_SNAPSHOT.md）：

- **`GET /media/keypoints/{token}`**：端点不存在；media 路由全文仅 `segment`/`init`。
- **`evidence` 响应的 `keypoints_url` / `detection` 字段**：不返回。
- **`keypoints_{ts_us}.json` 落盘 / token kind `keypoints`**：均已下线。
- **`client_id` / `source_ip`**：追溯链路不依赖，`evidence`/`playlist`/`timeline` 均用 alarm 自带 `(task_id, step_id)` 定位。

## 前端调用路径（端到端）

- **告警双轨复核**：`evidence` 拿双轨元数据 → 各请求一条 `alarm/{id}/playlist.m3u8?track=raw|processed` 喂 hls.js（自动经 `/media/init`+`/media/segment` 拉流）；用 `(clips[triggerIdx].ts_ms - clips[0].ts_ms)/1000` seek 到触发段。
- **单步骤完整回放 + 打点**：`task/{id}/playlist.m3u8?step_id=&track=` 播放 + `task/{id}/timeline?step_id=` 在进度条叠加告警标记。
- **告警跳转回放**：`evidence` 顶层 `task_id`/`step_id` 直接拼步骤回放 URL。

## 代码来源

- `app/routers/traceback.py`
- `app/routers/media.py`
- `app/services/traceback/segment_finder.py`
- `app/services/traceback/media_token.py`
- `tests/test_traceback_router.py`
- `tests/test_traceback_segment_finder.py`
- `tests/test_traceback_media_token.py`

