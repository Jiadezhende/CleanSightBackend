> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Lab Service

Lab 有三条路径：从一次 run 的 raw 轨裁剪区间送 Label Studio（ClipBuilder）；把某轨全部已落盘段 remux 成单个 mp4 供下载（StepExporter）；读离线模型的逐帧类别概率做可视化。

> 端点 schema、字段语义与错误码见 [docs/api/lab.md](../api/lab.md)。本文件只写路由归属、内部分工与能力边界。

## 路由

| 端点 | 归属 |
|------|------|
| `GET /lab-f3m8/tasks` | 可标注任务列表（数据源由 `task_source` 决定；每项带 `run_ids` 与 `offline_steps`） |
| `POST /lab-f3m8/label-probs` | 离线分割模型逐帧类别概率（`label_probs.npz`） |
| `POST /lab-f3m8/submit` | 区间裁剪 + 送标（`lab_service.submit_clips`） |
| `GET /lab-f3m8/download` | 整段导出下载（`lab_service.export_step`） |
| `GET /lab-f3m8/health` | LS 配置 + 可达性探测（`lab_service.ping_label_studio`） |
| `GET\|PUT /lab-f3m8/config` | LS 连接配置读写（`runtime_config`，持久化） |
| `/ui-f3m8/lab/` | 送标工作台（`app/static/lab/index.html`，经全局 `/ui-f3m8` 挂载，前端库取 `/ui-f3m8/vendor/`） |

`lab-f3m8` / `ui-f3m8` 都是混淆前缀，降低扫描器命中率（无登录）。`/ui-f3m8` 属网关普通档，见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。带 `(task_id, step_id, 可选 run_id)` 的端点在入口经 `app/routers/utils/runs.py` 解析一次 run，之后只用这个 `RunIdentity`（见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)「读侧 run 锁定」）。

## router 只管检查顺序与 DTO，流程在 service

`app/services/lab/__init__.py` 零 re-export，调用方走深路径（`from app.services.lab import service as lab_service`、`runtime_config as lab_config`）。

- **`service.py`**：无活体的模块函数——`require_label_studio` / `resolve_project_id` / `validate_clips` / `submit_clips` / `export_step` / `ping_label_studio`。ClipBuilder、StepExporter、LabelStudioClient 都在函数内按 `settings` 构造；全是阻塞调用，router 经 `run_in_threadpool` 调。`submit_clips` 不再校验，`clips` 必须是 `validate_clips` 的返回值。
- **`types.py`**：`ClipRange`（媒体坐标）、`ClipOutcome`（字段与 router 的 `LabClipResultDTO` 逐一同名）/ `SubmitOutcome`、单段失败码 `ERR_*`（对外字面量，改名即破坏前端契约）。
- **router（`app/routers/lab.py`）**：检查顺序、异常 → HTTP 映射、DTO 组装、任务列表拼装。「run 有没有 raw 段」的 404 留在 router，因为它依赖 `resolve_run`，而 services 不许 import routers。

## 送标区间收媒体坐标，响应带回墙钟

`/submit` 的区间是 `start_media_ms` / `end_media_ms`（= `<video>.currentTime × 1000`，相对该 run **raw 轨**媒体轴原点）。不收墙钟：浏览器按「首段墙钟 + currentTime」算，遇到断流会把区间整体推后 Σgap，裁出的 clip 时长对、能播、不报错，但不含操作员标的事件。坐标定义见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)「两套坐标：墙钟与媒体轴」。

响应每段带回墙钟 `start_ms` / `end_ms`（算不出时 null），产物文件名 `clip_{start_ms}_{end_ms}.mp4` 也用墙钟——媒体刻度随段集合变化，不能当持久标识。

`validate_clips` 先判数量（排序前），再按起点升序逐段判区间合法、单段时长、不重叠，最后判总时长；校验文案里的 `clip[i]` 是**排序后**下标。

## Submit：503 → 400 → 400 → 404 的检查顺序是对外契约

1. `require_label_studio()`：LS url / token 未配置 → 503。
2. `resolve_project_id()`：请求值优先（0 / None 视为未传），否则取运行时默认值；都没有 → 400。
3. `validate_clips()`：上限来自 `settings.lab_export_max_*` → 400。
4. `resolve_run` + `hls.query_has_segments(run, "raw")`：点名 run 不在 → 404 Run；无可见 run 或无 raw 段 → 404 Segments。
5. `submit_clips`：逐段 `ClipBuilder.build_one` 后上传 LS。单段失败不让整请求失败：HTTP 仍 200，每段带 `success` / `error_code`；LS 失败优先透传客户端的 `ls_unreachable` / `ls_auth` 等码，缺省 `ls_bad_response`。
6. 全成功或 `keep_artifacts_on_failure=false` → 删 job_dir；否则保留并回传路径。非 `ClipBuildError` 的异常直接冒出，job_dir 不清理。

⚠ `keep_artifacts_on_failure` 请求默认 `true`，而保留的 job_dir（`.lab_exports/{nonce}/`）没有任何自动回收：`_sweep_orphans` 只扫 `step_*.mp4`，CleanupWorker 不扫非数字目录。失败提交多了会在 `.lab_exports` 下累积。

## 任务列表：db 或 storage 两种来源，都只收有 raw 段的最新可见 run

来源由 `lab_config.get_task_source()` 决定（默认 `db`），经 `PUT /config` 的 `task_source` 切换，传 `null` 保持当前模式。两种模式都返回 `run_ids: {step_id: run_id}`（后续请求带上即锁定同一个 run）与 `offline_steps`（`query_has_offline_results(run)`：`temporal.jsonl` 里有 `TemporalSegment`，或 `label_probs.npz` 存在，npz 不解析）。

- **`db`**：`db_tasks.query_task_page(q, limit=, offset=)` 查 `clean_task`（按 `source_ip` / `status` / `task_id` 搜索）；session 关闭后再逐行扫盘补 raw 信息。
- **`storage`**：只以存储目录为来源（`tasks.list_task_ids()` → `_list_raw_runs`），不碰业务库。`q` 只按 `task_id` 子串过滤；DB 才有的字段不推断（`source_ip=None`、`status="unknown"`、`current_step` / `step_id` 为空）；`start_time` / `updated_time` 取各 run raw 轨 `query_span` 的 min(`start_ms`) / max(`end_ms`)，`end_ms` 是段尾而不是段起点。

`_list_raw_runs` = `runs.query_latest_by_step` 再过滤 `hls.query_has_segments(run, "raw")`。前者只看 run 可见（有 `detections.jsonl` 也算），这层过滤不能省，否则没写成段的 step 点开是黑屏。

⚠ **已知风险**：`list_lab_tasks` 是 `async def`，但函数体同步查 DB 并逐 task 扫盘，执行期间阻塞事件循环（同进程 `/ai/video` WS 一起卡）；影响程度未实测。

## `/label-probs` 只做可视化，没有产物返回空数组

同步 `def` 端点：`resolve_timeline`（该轨无段 → 404）→ `inference.read_label_probs(run)` → 每帧 `ts`（浮点秒）取 `round(ts × 1000)` 经 `timeline.media_ms_at` 换成该轨媒体刻度，概率按类转置成一类一条曲线。产物由离线作业写出，见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。

## 工作台可切 processed 查看，但打点与送标恒用 raw

纯前端能力（`index.html`），`/submit` 请求体没有 `track` 字段：processed 下打点按钮禁用；切轨必须重取 timeline（两轨 Σ EXTINF 与 `media_offset_ms` 不同尺）；切 processed 前先 `fetch` 预检其 playlist，失败则留在 raw。

## ClipBuilder：在清单上做纯减法

`clip_builder.py::build_one`：

1. 区间非法（end ≤ start）或超 `max_duration_ms` → `ClipBuildError`。
2. `hls.query_timeline(run, "raw").select(start, end)` 取相交段，空 → `ClipRangeOutOfBoundsError`。
3. `first_gap(window)` 有真实停顿 → `ClipRangeGapError`（`range_gap`）。
4. `window.wall_ms_at()` 换墙钟；区间尾越过轨尾时贴到末段段尾，`duration_ms` 随之收短。
5. `window.media_offset_ms(start)` 得 `-ss` 偏移：取子集时 ffmpeg 把首段 `tfdt` 归一到 0，偏移是纯减法。
6. `render_vod` 写临时 VOD 清单 `.clip_{nonce}.m3u8` 到该 run 的 `hls/`（条目是裸文件名，须与段、init 同目录），ffmpeg HLS demuxer 读入，libx264 重编码。缺 init → `ClipBuildError`。

硬约束：

- **段只从清单来**（`hls.query_timeline` / `list_segments`）：喂坏段或未登记段，ffmpeg exit 0、产出截短但合法的 mp4，returncode 抓不住。
- **不用 `-f concat`**：fMP4 fragment 无 moov，必须靠 `EXT-X-MAP` 带 init。
- **`-ss` 放在 `-i` 之后**（输出侧 seek）。模块 docstring 称挪到输入侧会 exit 0 产出 261 字节零流空壳，待核验。

临时清单 EXTINF 用清单真值（ffmpeg 时间轴来自 fragment 的 `tfdt`，改写 EXTINF 无效）。`.clip_*` / `.export_*` 不匹配段名 / init 名正则，读侧枚举天然跳过。成功判据与各失败形态见 [DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md#5-验收判据三种失败骗过-returncode--0)：两处调用点只判 returncode + 产物存在（不判 size），并带超时（ClipBuilder `max(60, 时长×4)` s）。

## 断流判据：墙钟间隙 > 500 ms

```text
gap = 下一段起点 − (本段起点 + EXTINF)       # MediaTimeline.wall_gaps()
真停顿 ⟺ gap > GAP_THRESHOLD_MS (500)       # app/services/utils/media_timeline.py
```

fps 漂移同时反映在起点与 EXTINF 上而被吸收。500 ms 落在正常残差（≈0 到几十 ms）与重连级断流（秒级）之间，不随段长或 fps 变。判据必须在墙钟轴上算：媒体轴上相邻段永远首尾相接。

## StepExporter：整段 `-c copy` remux，与 ClipBuilder 不合并

| | `POST /submit`（ClipBuilder） | `GET /download`（StepExporter） |
|---|---|---|
| 范围 | ms 精度媒体区间 | 整个 run 一轨 |
| 编码 | `-ss/-to` + libx264 重编码 | `-c copy` + `+faststart` |
| 轨 | 恒 raw | `raw` \| `processed`（默认 processed） |
| 去向 | Label Studio | HTTP attachment |

段落盘时已是 H.264 / yuv420p 的 fMP4 fragment（`app/storage/hls/_fmp4.py`），导出只需换容器。

- 与 ClipBuilder 同约束：段与时长只走 `hls.list_segments`（录制中的 run 下载得到已落盘部分）；不用 `-f concat`；临时清单 `.export_{nonce}.m3u8` 落该 run 的 `hls/`，用 `render_vod` 拼成带 `#EXT-X-ENDLIST` 的 VOD 形态——直接喂写入侧的 LIVE 清单，ffmpeg 会无限轮询等新段直到超时（超时 `max(120, 段数×5)` s）。
- 错误映射：`resolve_run` 为 None → 404（不建 `.lab_exports`）；`StepExportNoSegments` → 404；`StepExportInitMissing` → 503（与 traceback playlist 同码同措辞）；其余 `StepExportError` → 500。前两者是后者子类，先接子类。
- 产物落 `{storage_base_dir}/.lab_exports/`（`lab_export_temp_dir` 可覆写），响应发完由 `BackgroundTask` 删除；客户端中途断开时不保证执行，故 `export()` 开头先清超过 30 min 的 `step_*.mp4`。
- `.lab_exports` 不被 CleanupWorker 扫描，因为它只认两级十进制数字目录 `{task}/{step}/`。若把临时目录改成数字名，它会在 `cleanup_days` 后被当作过期 step 整个删掉，且无提示。
- 不做产物缓存（录制中的 run 段会增长），不限时长与体积。

## LabelStudioClient：单文件 multipart，不带任何附加字段

`label_studio_client.py`，urllib 实现：`ping()` → `GET /api/version`；`import_clip()` → `POST /api/projects/{project_id}/import`，multipart 单文件（整个 mp4 读进内存，clip 上限 5 min 可接受）。失败码：HTTP 401 / 403 → `ls_auth`；其他 HTTP 错误或响应里看不出已建 task → `ls_bad_response`；连接失败及其他异常 → `ls_unreachable`。

LS import 在文件上传模式下只读 `request.FILES`，非文件字段一律忽略，所以 clip 不带元数据；素材侧溯源只剩文件名里的墙钟区间。后端职责止于推 clip，不负责从 LS 导出标注或转训练集。

## 配置：url / project_id / task_source 可在页面改，token 只来自 env

- 运行时可改并持久化（`runtime_config.py`，`{storage_base_dir}/lab_runtime_config.json`，文件值优先、回退 env）：`label_studio_url`、`default_project_id`、`task_source`。
- LS token 只来自 `settings.label_studio_token`，页面不可见、不可改。`/health` 由 router 直接读 `lab_config.get_url / get_token / get_default_project_id`，再调 `ping_label_studio`（超时 10 s，异常折成 `(False, 消息)`，不抛）。
- 其余参数在 `settings`：`lab_export_temp_dir`（空则 `{storage_base_dir}/.lab_exports`）、`lab_export_ffmpeg_preset`、`lab_export_max_clip_ms` / `lab_export_max_total_ms` / `lab_export_max_clips_per_submit`。

## 代码来源

- `app/routers/lab.py`、`app/routers/utils/runs.py`
- `app/services/lab/{service,types,clip_builder,step_exporter,label_studio_client,runtime_config}.py`
- `app/db/tasks.py`（`query_task_page`）
- `app/storage/hls/`、`app/storage/runs.py`、`app/storage/tasks.py`、`app/storage/inference/`
- `app/services/utils/media_timeline.py`、`app/services/utils/vod_playlist.py`
- `app/static/lab/index.html`
- 测试见 [TESTING_MAP.md](TESTING_MAP.md)「Lab」
