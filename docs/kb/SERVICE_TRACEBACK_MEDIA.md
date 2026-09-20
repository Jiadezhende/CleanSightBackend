> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Traceback And Media Service

追溯服务按 `(task_id, step_id)` 定位 HLS 段，动态生成 VOD 清单与时间轴，并通过 token 化媒体路由返回可播放资源。

两层路由：`/traceback/*` 业务查询层返回带 HMAC token 的媒体 URL；`/media/*` 访问层校验 token 后返回文件。落盘按 `(task_id, step_id)` 隔离，链路不读 `source_ip`（`clean_alarm` 自带 `(task_id, step_id)`，可直接定位）。

> 端点的请求/响应 schema、字段语义与错误码属对外 API 文档：[docs/api/traceback.md](../api/traceback.md)、[docs/api/media.md](../api/media.md)。本文件只写路由归属、接线与能力边界。

## 数据底座（段枚举已收口到 `app.storage.hls`）

追溯层自己不认文件名、不遍历目录，四件事各有唯一出口：

| 能力 | 出口 | 说明 |
|------|------|------|
| 段枚举 | `hls.list_segments(task_id, step_id, track)` | **纯清单解析**：读写入侧 `{track}_playlist.m3u8`，一次同时给出「有哪些段」与逐段 EXTINF 真时长 |
| 区间段枚举 | `hls.list_segments_in_range(...)` | 同源、同返回类型，只取落在某墙钟区间内的那些 |
| 跨 task/step 枚举 | `app.storage.tasks` 的 `list_task_ids(order="id"\|"mtime")` / `list_step_ids(task_id)` / `delete_step(...)` | 把 step 目录当整体看的三件事 |
| VOD 清单骨架 | `app/services/utils/vod_playlist.py` 的 `render_vod(entries, map_uri=...)` | **不在 hls 域内**：它是给播放器/ffmpeg 消费的装配产物，不是段的元数据。URI 与时长都由调用方定好再交进来，该模块对 `MediaToken` 零认知 |
| 墙钟 ↔ 媒体轴换算 | `app/services/utils/media_timeline.py` 的 `MediaTimeline` | 见下节 |

**文件系统枚举已整个从 hls 域里删除**（不是降级为私有）：盘上有文件而清单没有条目的，是在途段或登记失败的残留，不是段——喂给播放器是 MSE 缓冲洞，喂给 ffmpeg 是静默截短。收口的直接后果：`playlist.m3u8` 端点原来「挑错 track」与「段全在途」两档 404 文案塌成一档，**不要为了保住分档再去 `iterdir` 一次**，那等于把第二个真源留回来。

`app/services/traceback/segment_finder.py`（`SegmentFinder` / `StepRef` / `parse_playlist_durations`）**已退役**：读的是升级前的 `{step}/` 平铺布局，现役写侧落 `{step}/hls/`，接上去只会读到空。`app/services/traceback/__init__.py` 刻意**不 re-export** 它——旧 `SegmentRef` 6 字段、新的 2 字段，混用不会立刻报错，不 re-export 才能让残留引用当场 `ImportError` 而不是静默拿到旧型。

## 两套坐标：墙钟与媒体轴

同一个 step 上并存两把尺，**混用就是静默算错**：

```
墙钟   段文件名里的 ts_us（采集那一刻决定）。回答"这件事几点发生的"——审计、检索、
       跨 step 对照、落库、送标产物命名。断流的那段时间在它上面是有宽度的。
媒体   Σ EXTINF，与 <video>.currentTime / duration 同源。回答"在播放器的第几秒"——
       进度条、告警标记落点、seek、ffmpeg -ss。**它是压紧的墙钟**：断流在它上面宽度为零。
```

`MediaTimeline` 是两者之间**唯一**的换算入口（`wall_ms_at` / `media_ms_at` / `select` / `media_offset_ms`）。换算必须读清单，所以只有后端做得了——浏览器手上只有媒体轴上的量，`首段墙钟 + currentTime` 这个恒等式**只在从没断过流时成立**，任何调用侧自凑都是 Σgap 的偏差。

`media_offset_ms` **随 track 变**：两轨各自独立切段，Σ EXTINF 不同尺，前端切轨必须重取 timeline。

落进空洞的墙钟由 `media_ms_at` 吸附到下一段段首——那段时间在媒体轴上没有对应刻度，吸到段首是唯一不撒谎的选择。

`MediaTimeline.load` 的累加**依赖 EXTINF 落盘精度是 ms 整数倍**（写侧 `_m3u8.entry()` 的 `:.3f`）；改了那个精度这里要换成整数累加，否则段间 ±1ms 错位、`select()` 边界漏段。

## MediaToken

媒体 URL 不暴露物理路径。`MediaToken` 生成 HMAC-SHA256 短 TTL token（`settings.media_token_ttl`，默认 300s），payload 为 `{"t": task_id, "s": step_id, "f": filename, "k": kind, "e": expiry}`。

- kind 只有两种：`segment` | `init`，**不可互换**（`verify(kind=...)` 强校验，防 segment token 换路由当 init 用）。
- `settings.media_token_secret` 为空时进程内生成临时随机密钥，重启后旧 token 全失效（会打一条 warning）。

## `/traceback/*`（业务层，2 个端点）

| 端点 | 归属函数 | 能力边界 |
|------|---------|---------|
| `GET\|HEAD /task/{task_id}/playlist.m3u8` | `get_task_playlist` | 单个 step 一轨的完整回放 VOD m3u8，动态生成。**`step_id` 必填，任务级跨 step 聚合不支持** |
| `GET /task/{task_id}/timeline` | `get_task_timeline` | 该 step 的时长 + 告警打点，墙钟与媒体两套坐标各给一份 |

**playlist 以 `@router.api_route(methods=["GET","HEAD"])` 注册（非 `@router.get`）**：FastAPI 的 `APIRoute` 不像 Starlette 原生 `Route` 那样给 GET 自动补 HEAD，漏注册即 405——既不合 RFC 9110，又会被 `GatewayMiddleware` 反扫描当扫描特征累计。原生 HLS 播放栈（Safari/AVPlayer/iOS WebView）取 playlist 前自动发 HEAD 探可用性；HEAD 照常执行 handler（扫段 + 拼清单后丢弃）给出正确 200/404，body 由 h11 在传输层抑制、`Content-Length` 保留真值。三档网关策略见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。

playlist 的两档错误**先后不能反**：先判「一个段都没有」→ 404，再判「有段但缺 `{track}_init.mp4`」→ 503。反过来的话，一个根本不存在的 task/step 会先撞上 503（「服务端暂时不可用、请重试」），对不存在的资源是误导。404 走 `NotFoundError` 而非裸 `HTTPException`——前者经全局处理器产出带 `resource_type`/`resource_id` 的结构化 body，换成裸的会让响应体形态静默塌陷。

timeline 的两类数据来源不同、**降级策略也不同**：段时长来自磁盘（权威），告警来自 DB；DB 不可用时退化为空 `events` 仍返回时长，不 503，DB 恢复即自愈。

`duration_ms` 取 **raw / processed 双轨并集**的墙钟跨度（`max(ts + EXTINF) − min(ts)`），与 `media_duration_ms`（**单轨** Σ EXTINF）**不同尺**。因此 `gap_total_ms` 必须由后端按逐段判据算，**不能用 `duration_ms − media_duration_ms` 去凑**：差值里混着「两轨起止不对齐」这一项（推理起步晚于取流时 processed 首段必然晚于 raw 首段），零断流的 step 也会算出假空洞。

## `/media/*`（访问层，2 个端点）

媒体路由只接受 token：

- `/media/segment/{token}`：返回 mp4 fragment，`Cache-Control: private, max-age=60`，`Content-Disposition: inline`。
- `/media/init/{token}`：返回 `{track}_init.mp4`（**按轨各一份**，同轨内所有段共享），`max-age=3600`；由 playlist 的 `#EXT-X-MAP` 自动签发。

**防路径穿越靠的是「名字必须解析得出身份键」而不是路径归一化**：token 里的 `filename` 先过 `hls.parse_segment_name` / `hls.parse_init_name`，解不出就 400 并记 warning（签发侧已拒过带分隔符的名字，走到这里说明 token 非本服务正常流程产物）；路径由 `hls.segment_path` / `hls.init_path` 从身份键重新拼出，调用方给的字符串不参与拼路径。init 的判据是 `parse_init_name` 而非 `endswith("init.mp4")`——后者会放行 `evil_init.mp4`。

## VOD playlist 原则

VOD 清单不直接 serve 落盘的 LIVE playlist，每次请求现生成（`render_vod`）：

- `#EXT-X-PLAYLIST-TYPE:VOD` + `#EXT-X-ENDLIST`：**缺 ENDLIST 会让 ffmpeg 当直播流只读 live edge，前面的段全丢**；播放器则一直轮询等新段。
- 每个段 URI 都是 token URL；`#EXT-X-MAP` 必填（fMP4 fragment 无 `EXT-X-MAP` 解不出 codec init，且是运行时才炸）。
- `EXTINF` 一律取清单真值，**不能用相邻段 `ts_us` 差重推**——那是墙钟量，断流时会把整个停顿算进段长。
- `TARGETDURATION` 取 `ceil(max EXTINF)` 下限 1（RFC 8216 要求它 ≥ 每段 EXTINF，用 `round` 会在段长 10.4s 时写出 10 而违规）。
- 空条目集抛 `ValueError`：「这个 step 还没有可播段」由 `hls.list_segments` 返回空列表表达，怎么映射成 HTTP 错误是调用方的判断。

## 已废弃 / 不再存在

避免误认为遗漏或试图调用：

- **`GET /traceback/alarm/{alarm_id}/evidence`** 与 **`GET /traceback/alarm/{alarm_id}/playlist.m3u8`**：两个端点连同只服务它们的下游代码、admin 面板的证据 UI 一并删除（2026-09-08）。`/traceback` 下现在只有 `/task/{id}/playlist.m3u8` 与 `/task/{id}/timeline`。**「按 alarm_id 一次取回证据」这个形态没有替代品**：告警定位回放改由 timeline + playlist 拼（见下节），调用方需自带 `(task_id, step_id)`。
- **`raw_clips[]` / `processed_clips[]` 这种裸 fragment URL 列表**：不再产出。双轨复核改为各请求一条 `playlist.m3u8?track=`。
- **`GET /media/keypoints/{token}`**、**token kind `keypoints`**、**`keypoints_{ts_us}.json` 落盘**：均已下线；media 路由全文仅 `segment` / `init`。
- **`settings.traceback_context_before` / `traceback_context_after`**：随 evidence 一并从 settings 删除，全仓无残留。
- **`client_id` / `source_ip`**：追溯链路不依赖；`playlist` / `timeline` 都用调用方给的 `(task_id, step_id)` 定位。

## 前端调用路径（端到端）

- **单步骤完整回放 + 告警打点**：`task/{id}/playlist.m3u8?step_id=&track=` 喂 hls.js（自动经 `/media/init` + `/media/segment` 拉流）+ `task/{id}/timeline?step_id=&track=` 在进度条叠加标记。**进度条全长用 `media_duration_ms`、标记落点用 `media_offset_ms`、播放头用 `currentTime`——三者同尺**；`start_ms`/`end_ms`/事件 `ts_ms` 只用于显示"几点发生的"。
- **切轨查看**：先 `fetch` 预检目标轨 playlist（某些 step 只有 raw），通过后换源 + 重取 timeline（媒体坐标按轨算）。
- **告警定位回放**：从 `GET /task/{task_id}/alarms`（或告警推送）拿到 `(task_id, step_id)` → 拼上面两条 URL；具体某条告警在画面里的落点由 timeline 的 `events[].media_offset_ms` 给。

## 代码来源

- `app/routers/traceback.py`
- `app/routers/media.py`
- `app/services/traceback/media_token.py`
- `app/services/utils/media_timeline.py`
- `app/services/utils/vod_playlist.py`
- `app/storage/hls/`（`_read.list_segments` / `_layout` 定位与命名）
- `app/storage/tasks.py`
- `tests/test_traceback_router.py`
- `tests/test_traceback_media_token.py`
- `tests/test_media_timeline.py`
- `tests/test_utils_vod_playlist.py`
- `tests/test_storage_hls.py`
