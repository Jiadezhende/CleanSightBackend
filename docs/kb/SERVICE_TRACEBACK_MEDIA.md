> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Traceback And Media Service

追溯服务按 `(task_id, step_id, 可选 run_id)` 定位一次 run 的 HLS 段，动态生成 VOD 清单与时间轴，并通过 token 化媒体路由返回可播放资源。

两层路由：`/traceback/*` 业务查询层返回带 HMAC token 的媒体 URL；`/media/*` 访问层校验 token 后返回文件。**入口解析一次 run、整次请求锁定**：`resolve_run(task, step, run_id?)` 得到 `RunIdentity` 后，整个请求只读这个 run 的 `{task}/{step}/{run_id}/hls/`；缺省 `run_id` = 该 step 最新可见 run。链路不读 `source_ip`（`clean_alarm` 自带 `(task_id, step_id)`，可直接定位）。

追溯没有独立的 service 包：run 解析与 `MediaToken` 是 HTTP 侧工具，放在 `app/routers/utils/`；router 不持有 DB session，告警经 `app.db.alarms` 的查询函数读取。

> 端点的请求/响应 schema、字段语义与错误码属对外 API 文档：[docs/api/traceback.md](../api/traceback.md)、[docs/api/media.md](../api/media.md)。本文件只写路由归属、接线与能力边界。

## 数据底座（段枚举已收口到 `app.storage.hls`）

追溯层自己不认文件名、不遍历目录、不开 DB session，每件事各有唯一出口：

| 能力 | 出口 | 说明 |
|------|------|------|
| run 解析 | `app/routers/utils/runs.py`：`resolve_run` / `no_run` / `resolve_timeline` / `resolve_media_run` | 全部落到 `app.storage.runs.query`；语义见下节 |
| 段枚举 | `hls.list_segments(run, track)` | **纯清单解析**：读写入侧 `{track}_playlist.m3u8`，一次同时给出「有哪些段」与逐段 EXTINF 真时长 |
| 区间段枚举 | `hls.list_segments_in_range(run, track, ...)` | 同源、同返回类型，只取落在某墙钟区间内的那些 |
| init 在不在 | `hls.query_has_init(run, track)` | 只判盘上存在；要路径用 `hls.init_path` |
| 墙钟跨度 | `hls.query_span(run, tracks=TRACKS)` → `HlsSpan(tracks, start_ms, last_start_ms, end_ms)` | 若干轨段跨度的并集，`end_ms = max(段起点 + round(EXTINF))`；全无段 → `None`。段跨度算法只此一份（task / lab 也用它） |
| run 存续区间 | `runs.query_lifespan_ms(run)` → `(run_id, successor 或 None)` | 「run_id = 分配时刻毫秒」这条知识只在 `app/storage/runs.py` |
| 墙钟 ↔ 媒体轴换算 | `hls.query_timeline(run, track)` → `hls.MediaTimeline`（`app/storage/hls/_read.py` / `_timeline.py`） | 见「两套坐标」；断流阈值 `GAP_THRESHOLD_MS` / `first_gap` / `total_gap_ms` 在 `app/services/utils/media_timeline.py` |
| VOD 清单骨架 | `app/services/utils/vod_playlist.py` 的 `render_vod(entries, map_uri=...)` | **不在 hls 域内**：它是给播放器/ffmpeg 消费的装配产物，不是段的元数据。URI 与时长都由调用方定好再交进来，该模块对 `MediaToken` 零认知 |
| 告警 | `db_alarms.query_step_alarms(task_id, step_id)`（detected_at 升序）+ `db_alarms.detected_at_ms`（`app/db/alarms.py`） | 查询函数自开自关 session，失败抛 `DatabaseError`；降级由调用方定 |

**文件系统枚举已整个从 hls 域里删除**（不是降级为私有）：盘上有文件而清单没有条目的，是在途段或登记失败的残留，不是段——喂给播放器是 MSE 缓冲洞，喂给 ffmpeg 是静默截短。收口的直接后果：`playlist.m3u8` 端点「挑错 track」与「段全在途」两档 404 文案塌成一档，**不要为了保住分档再去 `iterdir` 一次**，那等于把第二个真源留回来。

## 读侧 run 锁定：`app/routers/utils/runs.py`

| 函数 | 语义 | 调用方 |
|------|------|--------|
| `resolve_run(task, step, run_id)` | 点名的 run 不在 → `NotFoundError(resource_type="Run")`（404）；缺省且无可见 run → `None`，由端点维持原「该 step 没数据」响应 | traceback、lab（submit / download）、admin 离线提交 |
| `no_run(task, step)` | 端点要求必须有 run 时，`None` 分支抛的 404 Run | admin 离线提交 |
| `resolve_timeline(task, step, run_id, track)` | `resolve_run` + `hls.query_timeline`；无可见 run 或该轨无段 → 404 Segments | `/ai/temporal`、`/lab-f3m8/label-probs` |
| `resolve_media_run(payload)` | token 锁定的 run；缺 `"r"` 的 token 按最新可见 run；run 已回收 / 不存在 → `HTTPException(404, "Media file not found")` | `/media/*` |

- `resolve_media_run` 不并进 `resolve_run`：媒体路由对「run 不在」一律回裸 `HTTPException` 的 "Media file not found"，不分点名 / 缺省，也不走 `NotFoundError` 的结构化错误体。
- 缺省 `run_id` 时各请求**各自**解析最新 run：播放途中同 step 重跑，不带 `run_id` 的 playlist 与 timeline 可能取自不同 run（半新半旧）。锁定办法是把列表接口给出的 `run_id` 带上。
- 同 step 重启后存在「新 run 因 `detections.jsonl` ~1s 就可见、首段 hls 要 10s+」的窗口：这期间不带 `run_id` 的回放得到 404 / 全 0，不回落上一次录像（接受的行为）。可见判据见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。

## 两套坐标：墙钟与媒体轴

同一个 run 的一条轨上并存两把尺，**混用就是静默算错**：

```
墙钟   段文件名里的 ts_ms（采集那一刻决定）。回答"这件事几点发生的"——审计、检索、
       跨 step 对照、落库、送标产物命名。断流的那段时间在它上面是有宽度的。
媒体   Σ EXTINF，与 <video>.currentTime / duration 同源。回答"在播放器的第几秒"——
       进度条、告警标记落点、seek、ffmpeg -ss。**它是压紧的墙钟**：断流在它上面宽度为零。
```

`MediaTimeline`（`app.storage.hls`，由 `hls.query_timeline` 构造）是两者之间**唯一**的换算入口（`wall_ms_at` / `media_ms_at` / `select` / `media_offset_ms`，另有不带阈值的 `wall_gaps()`）。换算必须读清单，所以只有后端做得了——浏览器手上只有媒体轴上的量，`首段墙钟 + currentTime` 这个恒等式**只在从没断过流时成立**，任何调用侧自凑都是 Σgap 的偏差。

`media_offset_ms` **随 track 变**：两轨各自独立切段，Σ EXTINF 不同尺，前端切轨必须重取 timeline。

落进空洞的墙钟由 `media_ms_at` 吸附到下一段段首——那段时间在媒体轴上没有对应刻度，吸到段首是唯一不撒谎的选择。

`hls.query_timeline` 的累加**依赖 EXTINF 落盘精度是 ms 整数倍**（写侧 `_m3u8.entry()` 的 `:.3f`）；改了那个精度这里要换成整数累加，否则段间 ±1ms 错位、`select()` 边界漏段。

## MediaToken

媒体 URL 不暴露物理路径。`MediaToken`（`app/routers/utils/media_token.py`）生成 HMAC-SHA256 短 TTL token（`settings.media_token_ttl`，默认 300s），payload 为 `{"t": task_id, "s": step_id, "r": run_id, "f": filename, "k": kind, "e": expiry}`，其中 `"r"` 可选。

- **playlist 签发时解析一次 run，清单里所有段与 init 的 token 都签同一个 `run_id`**：播放途中同 step 换代，段与 init 仍取自签发时的 run。缺 `"r"` 的 token 由 `resolve_media_run` 按最新可见 run 解析；run 已被 TTL 回收 → 404。
- kind 只有两种：`segment` | `init`，**不可互换**（`verify(kind=...)` 强校验，防 segment token 换路由当 init 用）。
- `settings.media_token_secret` 为空时进程内生成临时随机密钥，重启后旧 token 全失效（会打一条 warning）。

## `/traceback/*`（业务层，2 个端点）

| 端点 | 归属函数 | 能力边界 |
|------|---------|---------|
| `GET\|HEAD /task/{task_id}/playlist.m3u8` | `get_task_playlist` | 单个 run 一轨的完整回放 VOD m3u8，动态生成。**`step_id` 必填、`run_id` 可选，任务级跨 step 聚合不支持** |
| `GET /task/{task_id}/timeline` | `get_task_timeline` | 该 run 的时长 + **该 run 存续期 `[run_id, successor)` 内**的告警打点，墙钟与媒体两套坐标各给一份 |

点名的 run 不在 → 404 `resource_type="Run"`；缺省且无可见 run → playlist 404 Segments、timeline 坐标全 0 且告警不过滤。

**playlist 以 `@router.api_route(methods=["GET","HEAD"])` 注册（非 `@router.get`）**：FastAPI 的 `APIRoute` 不像 Starlette 原生 `Route` 那样给 GET 自动补 HEAD，漏注册即 405——既不合 RFC 9110，又会被 `GatewayMiddleware` 反扫描当扫描特征累计。原生 HLS 播放栈（Safari/AVPlayer/iOS WebView）取 playlist 前自动发 HEAD 探可用性；HEAD 照常执行 handler（扫段 + 拼清单后丢弃）给出正确 200/404，body 由 h11 在传输层抑制、`Content-Length` 保留真值。三档网关策略见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。

playlist 的两档错误**先后不能反**：先判「一个段都没有」（`hls.list_segments` 为空）→ 404，再判「有段但缺 `{track}_init.mp4`」（`hls.query_has_init`）→ 503。反过来的话，一个根本不存在的 task/step 会先撞上 503（「服务端暂时不可用、请重试」），对不存在的资源是误导。run 目录全由现行代码写出，缺 init 只剩「首段仍在 transcode 途中」一种可能。404 走 `NotFoundError` 而非裸 `HTTPException`——前者经全局处理器产出带 `resource_type`/`resource_id` 的结构化 body，换成裸的会让响应体形态静默塌陷。

### timeline 的接线

```text
run      = resolve_run(task, step, run_id)
span     = hls.query_span(run)                  双轨并集墙钟跨度；None → start/end/duration 全 0
timeline = hls.query_timeline(run, track)       媒体轴；gap_total_ms = total_gap_ms(timeline)
lo, hi   = runs.query_lifespan_ms(run)          告警过滤区间 [lo, hi)，最新 run 无上界
alarms   = db_alarms.query_step_alarms(task, step)
           逐条 detected_at_ms 归一（NULL 跳过；≤0 抛 ValidationError → 整次 400）
           落在 [lo, hi) 外的丢弃；media_offset_ms = timeline.media_ms_at(ts_ms)
```

- 告警过滤规则留在 router；DB 告警本身无 run 维度，区间两端取自盘上的 run_id。结算告警在 `stop_run` 拆除时生成、重启是先 stop 再 allocate，故一定落在本 run 区间内。
- 两类数据来源不同、**降级策略也不同**：段时长来自磁盘（权威），告警来自 DB；`DatabaseError` 时退化为空 `events` 仍返回时长，不 503，DB 恢复即自愈。

`duration_ms` 取 **raw / processed 双轨并集**的墙钟跨度（`HlsSpan.end_ms − start_ms`），与 `media_duration_ms`（**单轨** Σ EXTINF）**不同尺**。因此 `gap_total_ms` 必须由后端按逐段判据算，**不能用 `duration_ms − media_duration_ms` 去凑**：差值里混着「两轨起止不对齐」这一项（推理起步晚于取流时 processed 首段必然晚于 raw 首段），零断流的 run 也会算出假空洞。

## `/media/*`（访问层，2 个端点）

媒体路由只接受 token：

- `/media/segment/{token}`：返回 mp4 fragment，`Cache-Control: private, max-age=60`，`Content-Disposition: inline`。
- `/media/init/{token}`：返回 `{track}_init.mp4`（**按轨各一份**，同轨内所有段共享），`max-age=3600`；由 playlist 的 `#EXT-X-MAP` 自动签发。

**防路径穿越靠的是「名字必须解析得出身份键」而不是路径归一化**：token 里的 `filename` 先过 `hls.parse_segment_name` / `hls.parse_init_name`，解不出就 400 并记 warning（签发侧已拒过带分隔符的名字，走到这里说明 token 非本服务正常流程产物）；路径由 `hls.segment_path(run, ref)` / `hls.init_path(run, track)` 从 `resolve_media_run` 得到的 run 与身份键重新拼出，调用方给的字符串不参与拼路径。init 的判据是 `parse_init_name` 而非 `endswith("init.mp4")`——后者会放行 `evil_init.mp4`。文件不在盘上 → 404（区别于名字非法的 400）。

## VOD playlist 原则

VOD 清单不直接 serve 落盘的 LIVE playlist，每次请求现生成（`render_vod`）：

- `#EXT-X-PLAYLIST-TYPE:VOD` + `#EXT-X-ENDLIST`：**缺 ENDLIST 会让 ffmpeg 当直播流只读 live edge，前面的段全丢**；播放器则一直轮询等新段。
- 每个段 URI 都是 token URL；`#EXT-X-MAP` 必填（fMP4 fragment 无 `EXT-X-MAP` 解不出 codec init，且是运行时才炸）。
- `EXTINF` 一律取清单真值，**不能用相邻段 `ts_ms` 差重推**——那是墙钟量，断流时会把整个停顿算进段长。
- `TARGETDURATION` 取 `ceil(max EXTINF)` 下限 1（RFC 8216 要求它 ≥ 每段 EXTINF，用 `round` 会在段长 10.4s 时写出 10 而违规）。
- 空条目集抛 `ValueError`：「这个 run 还没有可播段」由 `hls.list_segments` 返回空列表表达，怎么映射成 HTTP 错误是调用方的判断。

## 已废弃 / 不再存在

避免误认为遗漏或试图调用：

- **`GET /traceback/alarm/{alarm_id}/evidence`** 与 **`GET /traceback/alarm/{alarm_id}/playlist.m3u8`**：两个端点连同只服务它们的下游代码、admin 面板的证据 UI 一并删除。`/traceback` 下现在只有 `/task/{id}/playlist.m3u8` 与 `/task/{id}/timeline`。**「按 alarm_id 一次取回证据」这个形态没有替代品**：告警定位回放改由 timeline + playlist 拼（见下节），调用方需自带 `(task_id, step_id)`。
- **`raw_clips[]` / `processed_clips[]` 这种裸 fragment URL 列表**：不再产出。双轨复核改为各请求一条 `playlist.m3u8?track=`。
- **`GET /media/keypoints/{token}`**、**token kind `keypoints`**、**`keypoints_*.json` 落盘**：均已下线；media 路由全文仅 `segment` / `init`。
- **`settings.traceback_context_before` / `traceback_context_after`**：随 evidence 一并从 settings 删除，全仓无残留。
- **`client_id` / `source_ip`**：追溯链路不依赖；`playlist` / `timeline` 都用调用方给的 `(task_id, step_id[, run_id])` 定位。

## 前端调用路径（端到端）

- **单次 run 完整回放 + 告警打点**：`task/{id}/playlist.m3u8?step_id=&track=&run_id=` 喂 hls.js（自动经 `/media/init` + `/media/segment` 拉流）+ `task/{id}/timeline?step_id=&track=&run_id=` 在进度条叠加标记。**进度条全长用 `media_duration_ms`、标记落点用 `media_offset_ms`、播放头用 `currentTime`——三者同尺**；`start_ms`/`end_ms`/事件 `ts_ms` 只用于显示"几点发生的"。
- **`run_id` 从哪来**：`/task/history` 的 `steps[].run_id`、`/task/live` 的 `run_id`、lab `/tasks` 的 `run_ids: {step_id: run_id}`。当前 `app/static/lab/index.html` 与 `app/static/admin/index.html` 请求 playlist / timeline **都不带 `run_id`**，落在上文「各请求各自解析最新 run」的情形。
- **切轨查看**：先 `fetch` 预检目标轨 playlist（某些 run 只有 raw），通过后换源 + 重取 timeline（媒体坐标按轨算）。
- **告警定位回放**：从 `GET /task/{task_id}/alarms`（或告警推送）拿到 `(task_id, step_id)` → 拼上面两条 URL；具体某条告警在画面里的落点由 timeline 的 `events[].media_offset_ms` 给。

## 代码来源

- `app/routers/traceback.py`
- `app/routers/media.py`
- `app/routers/utils/runs.py`、`app/routers/utils/media_token.py`
- `app/db/alarms.py`（`query_step_alarms` / `detected_at_ms`）
- `app/storage/hls/`（`_read.list_segments` / `query_span` / `query_timeline` / `query_has_init`、`_timeline` 媒体轴、`_layout` 定位与命名、`types.HlsSpan`）
- `app/storage/runs.py`（`query` / `query_lifespan_ms`）
- `app/services/utils/media_timeline.py`（断流阈值 `GAP_THRESHOLD_MS` / `total_gap_ms`）
- `app/services/utils/vod_playlist.py`
- `tests/test_traceback_router.py`
- `tests/test_router_utils_runs.py`
- `tests/test_media_token.py`
- `tests/test_media_timeline.py`（只测断流判据）
- `tests/test_utils_vod_playlist.py`
- `tests/test_storage_hls.py`（媒体轴、`query_span` / `query_has_init`）
