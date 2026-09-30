> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Traceback And Media Service

追溯按 `(task_id, step_id, 可选 run_id)` 定位一次 run 的 HLS 段，现生成 VOD 清单与时间轴，经 token 化媒体路由返回资源。

- **两层路由**：`/traceback/*` 业务查询层返回带 HMAC token 的媒体 URL；`/media/*` 访问层校验 token 后返回文件。
- **入口解析一次 run、整次请求锁定**：`resolve_run` 得到 `RunIdentity` 后只读 `{task}/{step}/{run_id}/hls/`；缺省 `run_id` = 该 step 最新可见 run。
- **没有独立 service 包**：run 解析与 `MediaToken` 是 HTTP 侧工具，在 `app/routers/utils/`；router 不持 DB session，告警经 `app.db.alarms` 查询函数读取。链路不读 `source_ip`。

端点的请求/响应 schema、字段语义与错误码见 [docs/api/traceback.md](../api/traceback.md)、[docs/api/media.md](../api/media.md)。

## 每件事只有一个出口

| 能力 | 出口 | 说明 |
|------|------|------|
| run 解析 | `app/routers/utils/runs.py` | 见下节，全部落到 `app.storage.runs.query` |
| 段枚举 | `hls.list_segments(run, track)` | 纯清单解析，同时给出段与逐段 EXTINF |
| init 在不在 | `hls.query_has_init(run, track)` | 只判存在；要路径用 `hls.init_path` |
| 墙钟跨度 | `hls.query_span(run, tracks=TRACKS)` → `HlsSpan(tracks, start_ms, last_start_ms, end_ms)` | 各轨并集，`end_ms = max(段起点 + round(EXTINF))`；无段 → `None`。task / lab 同用 |
| run 存续区间 | `runs.query_lifespan_ms(run)` → `(run_id, successor 或 None)` | 「run_id = 分配时刻毫秒」只在 `app/storage/runs.py` |
| 墙钟↔媒体换算 | `hls.query_timeline(run, track)` → `MediaTimeline` | 断流阈值在 `app/services/utils/media_timeline.py` |
| VOD 清单骨架 | `app/services/utils/vod_playlist.py::render_vod(entries, map_uri=)` | 不在 hls 域内（装配产物，非段元数据）；URI 与时长由调用方定好，对 `MediaToken` 零认知 |
| 告警 | `db_alarms.query_step_alarms(task_id, step_id)`（detected_at 升序）+ `db_alarms.detected_at_ms` | 自开自关 session，失败抛 `DatabaseError`，降级由调用方定 |

盘上有文件而清单无条目的不是段（理由见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md) §5）。因此 playlist 的 404 只有一档，文案同时提示「挑错 track / 首段仍在转码」。**不要为了分档再 `iterdir`**，那会把第二个真源带回来。

## 读侧 run 锁定：`app/routers/utils/runs.py`

| 函数 | 语义 | 调用方 |
|------|------|--------|
| `resolve_run(task, step, run_id)` | 点名的 run 不在 → `NotFoundError(resource_type="Run")`（404）；缺省且无可见 run → `None`，由端点维持原「该 step 没数据」响应 | traceback、lab（submit / download）、admin 离线作业（提交 / 查询） |
| `no_run(task, step)` | 端点要求必须有 run 时，`None` 分支抛的 404 Run | admin 离线提交 |
| `resolve_timeline(task, step, run_id, track)` | `resolve_run` + `hls.query_timeline`；无可见 run 或该轨无段 → 404 Segments | `/ai/temporal`、`/lab-f3m8/label-probs` |
| `resolve_media_run(payload)` | token 锁定的 run；缺 `"r"` 按最新可见 run；run 已回收或不存在 → 裸 `HTTPException(404, "Media file not found")` | `/media/*` |

- `resolve_media_run` 不并进 `resolve_run`：媒体路由对「run 不在」一律回同一个裸 404，不分点名 / 缺省，也不走 `NotFoundError` 的结构化错误体。
- 缺省 `run_id` 时各请求**各自**解析最新 run：播放途中同 step 重跑，不带 `run_id` 的 playlist 与 timeline 可能取自不同 run。锁定办法是带上列表接口给出的 `run_id`。
- 同 step 重启后，新 run 在 `detections.jsonl` 落盘前（约 1s）不可见，这期间不带 `run_id` 的查询仍落到上一个 run；之后新 run 可见，但首段 hls 要 10s+，这段窗口里回放得到 404 / 全 0，不回落上一次录像（接受的行为）。可见判据见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。

## 两套坐标：换算只在后端做

```text
墙钟   段文件名 ts_ms。回答「几点发生的」——审计、检索、跨 step 对照、落库、送标产物命名。断流在它上面有宽度。
媒体   Σ EXTINF，与 <video>.currentTime / duration 同源。回答「播放器第几秒」——进度条、告警落点、seek、ffmpeg -ss。
       它是压紧的墙钟，断流宽度为零。
```

`MediaTimeline` 是唯一换算入口（`wall_ms_at` / `media_ms_at` / `select` / `media_offset_ms` / 不带阈值的 `wall_gaps()`）。浏览器手上只有媒体量，`首段墙钟 + currentTime` 只在从没断流时成立，所以换算必须在后端读清单做。两轨各自切段、媒体轴不同尺，前端切轨必须重取 timeline。空洞吸附、精度前提等原则见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md) §4。

## MediaToken

`MediaToken`（`app/routers/utils/media_token.py`）签 HMAC-SHA256 短 TTL token（`settings.media_token_ttl`，默认 300s），payload 为 `{"t": task_id, "s": step_id, "r": run_id, "f": filename, "k": kind, "e": expiry}`，`"r"` 可选。

- **playlist 签发时解析一次 run，清单里所有段与 init 的 token 都签同一个 `run_id`**：播放途中换代，段与 init 仍取自签发时的 run。
- kind 只有 `segment` | `init`，`verify(kind=...)` 强校验，防止 segment token 换路由当 init 用。
- 签发侧拒绝含 `/`、`\` 或为 `.`/`..` 的 filename。
- `settings.media_token_secret` 为空时进程内生成临时随机密钥并打 warning，重启后旧 token 全失效。

## `/traceback/*`（业务层，2 个端点）

| 端点 | 归属函数 | 能力边界 |
|------|---------|---------|
| `GET\|HEAD /task/{task_id}/playlist.m3u8` | `get_task_playlist` | 单个 run 一轨的完整 VOD m3u8，现生成，`Cache-Control: no-store`。`step_id` 必填、`run_id` 可选、`track` 缺省 `processed`；不支持跨 step 聚合 |
| `GET /task/{task_id}/timeline` | `get_task_timeline` | 该 run 的时长 + 存续期 `[run_id, successor)` 内的告警打点，墙钟与媒体坐标各一份；`track` 缺省 `raw` |

点名的 run 不在 → 404 `resource_type="Run"`；缺省且无可见 run → playlist 404 Segments，timeline 坐标全 0、告警不过滤。

### playlist 必须同时注册 HEAD

用 `@router.api_route(methods=["GET","HEAD"])`，因为 FastAPI `APIRoute` 不像 Starlette `Route` 那样给 GET 自动补 HEAD。漏注册会返回 405：既不合 RFC 9110，又会被 `app/gateway.py` 的反扫描当 404/405 特征计数（300s 内 10 次封 IP 1h）。原生 HLS 播放栈（Safari / AVPlayer / iOS WebView）取 playlist 前会自动发 HEAD。HEAD 照常执行 handler，给出正确的 200/404；body 由 h11 在传输层抑制，`Content-Length` 保留真值。网关分档见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。

### playlist 先判 404 再判 503

先判「一个段都没有」→ 404，再判「有段但缺 `{track}_init.mp4`」→ 503，顺序不能反（原则见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md) §6）。写侧 commit 时 init 先于清单条目，所以清单非空时 init 必在；503 只在 init 被外部删除时触发。404 走 `NotFoundError` 而非裸 `HTTPException`：前者经全局处理器产出带 `resource_type` / `resource_id` 的结构化 body，换成裸的会让响应体形态静默塌陷。

### timeline 的接线

```text
run      = resolve_run(task, step, run_id)
span     = hls.query_span(run)                  双轨并集墙钟跨度；None → start/end/duration 全 0
timeline = hls.query_timeline(run, track)       媒体轴；gap_total_ms = total_gap_ms(timeline)
lo, hi   = runs.query_lifespan_ms(run)          告警过滤区间 [lo, hi)，最新 run 无上界
alarms   = db_alarms.query_step_alarms(task, step)
           detected_at 为 NULL 跳过；detected_at_ms 归一（秒/毫秒/微秒按量级判），≤0 抛 ValidationError → 整次 400
           落在 [lo, hi) 外的丢弃；media_offset_ms = timeline.media_ms_at(ts_ms)
```

- 告警本身无 run 维度，过滤区间两端取自盘上的 run_id。结算告警在 `stop_run` 拆除时生成，重启是先 stop 再 allocate，所以一定落在本 run 区间内。
- 两类数据降级策略不同：段时长来自磁盘（权威）；告警来自 DB，`DatabaseError` 时退化为空 `events`，仍返回时长，不 503，DB 恢复即自愈。
- `duration_ms` 是双轨并集墙钟跨度，`media_duration_ms` 是单轨 Σ EXTINF，二者不同尺。`gap_total_ms` 必须由后端按逐段判据算，不能用两者之差凑（理由见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md) §4）。

## `/media/*`（访问层，2 个端点）

| 端点 | 返回 | 缓存 |
|------|------|------|
| `/media/segment/{token}` | mp4 fragment，`Content-Disposition: inline` | `private, max-age=60` |
| `/media/init/{token}` | `{track}_init.mp4`（按轨各一份），由 playlist 的 `#EXT-X-MAP` 签发 | `private, max-age=3600` |

token 无效或过期 → 403。**防路径穿越靠「名字必须解析得出身份键」，而不是路径归一化**：

- token 里的 `filename` 先过 `hls.parse_segment_name` / `hls.parse_init_name`，解不出就 400 并记 warning；
- 路径由 `hls.segment_path(run, ref)` / `hls.init_path(run, track)` 重新拼出，调用方给的字符串不参与；
- init 判据是 `parse_init_name` 而非 `endswith("init.mp4")`，后者会放行 `evil_init.mp4`；
- 文件不在盘上 → 404（区别于名字非法的 400）。

## VOD 清单每次现生成，不 serve 落盘的 LIVE 清单

- 必须 `#EXT-X-PLAYLIST-TYPE:VOD` + `#EXT-X-ENDLIST`。缺 ENDLIST 时，ffmpeg 当直播流无限轮询、挂死到超时（实测，见 [DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md) §5.2），播放器则一直轮询等新段。
- 每个段 URI 都是 token URL。`#EXT-X-MAP` 必填（`map_uri` 无默认），fMP4 fragment 没有它就解不出 codec init，而且要到运行时才炸。
- `EXTINF` 一律取清单真值，不能用相邻段 `ts_ms` 差重推：那是墙钟量，断流时会把停顿算进段长。
- `TARGETDURATION` 取 `ceil(max EXTINF)`，下限 1。RFC 8216 要求它 ≥ 每段 EXTINF，用 `round` 会在段长 10.4s 时写出 10 而违规。
- 空条目集抛 `ValueError`；「还没有可播段」由 `hls.list_segments` 返回空列表表达，映射成什么 HTTP 错误由调用方决定。

## 前端调用路径

- **单次 run 回放 + 告警打点**：`playlist.m3u8?step_id=&track=&run_id=` 喂 hls.js，自动经 `/media/init` + `/media/segment` 拉流；`timeline?step_id=&track=&run_id=` 在进度条叠加标记。**进度条全长用 `media_duration_ms`、标记落点用 `media_offset_ms`、播放头用 `currentTime`，三者同尺**；`start_ms` / `end_ms` / 事件 `ts_ms` 只用来显示「几点发生的」。
- **`run_id` 从哪来**：`/task/history` 的 `steps[].run_id`、`/task/live` 的 `run_id`、lab `/tasks` 的 `run_ids: {step_id: run_id}`。当前 `app/static/lab/index.html`（playlist + timeline）与 `app/static/admin/index.html`（playlist）**都不带 `run_id`**，落在「各请求各自解析最新 run」的情形。
- **切轨查看**：先 `fetch` 预检目标轨 playlist（有些 run 只有 raw），通过后换源并重取 timeline。
- **告警定位回放**：从 `GET /task/{task_id}/alarms`（或告警推送）拿到 `(task_id, step_id)` 后拼上面两条 URL；告警在画面里的落点由 `events[].media_offset_ms` 给出。没有「按 alarm_id 一次取回证据」的端点。

## 代码来源

- `app/routers/traceback.py`、`app/routers/media.py`
- `app/routers/utils/runs.py`、`app/routers/utils/media_token.py`
- `app/db/alarms.py`（`query_step_alarms` / `detected_at_ms`）
- `app/storage/hls/`（`_read`、`_timeline`、`_layout`、`types.HlsSpan`）、`app/storage/runs.py`
- `app/services/utils/media_timeline.py`、`app/services/utils/vod_playlist.py`
- `tests/test_traceback_router.py`、`tests/test_router_utils_runs.py`、`tests/test_media_token.py`、`tests/test_media_timeline.py`、`tests/test_utils_vod_playlist.py`、`tests/test_storage_hls.py`
