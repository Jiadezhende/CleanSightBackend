> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Lab Service

Lab 服务从一次 run 的 raw HLS 段裁剪样本视频并提交到 Label Studio 创建标注任务；另有一条不经 LS 的**整段导出**旁路，把某轨已落盘的全部段 remux 成单个 mp4 供下载；以及离线模型逐帧类别概率的可视化读口。

> 端点的请求/响应 schema、字段语义与错误码见 [docs/api/lab.md](../api/lab.md)。本文件只写路由归属、内部分工与能力边界。

## 路由

| 端点 | 归属 |
|------|------|
| `GET /lab-f3m8/tasks` | 可标注任务列表（数据源由运行时 `task_source` 开关决定；每项带 `run_ids` 与 `offline_steps`） |
| `POST /lab-f3m8/label-probs` | 离线分割模型逐帧类别概率（`label_probs.npz`，可视化旁路），帧 ts 经 `resolve_timeline` 换算到媒体刻度 |
| `POST /lab-f3m8/submit` | 区间裁剪 + 送标（`lab_service.submit_clips`，内部用 ClipBuilder + LabelStudioClient） |
| `GET /lab-f3m8/download` | 整段导出下载（`lab_service.export_step`，内部用 StepExporter） |
| `GET /lab-f3m8/health` | LS 配置 + 可达性探测（`lab_service.ping_label_studio`） |
| `GET\|PUT /lab-f3m8/config` | LS 连接配置读写（`runtime_config`，持久化） |
| `/ui-f3m8/lab/` | 送标工作台（`app/static/lab/index.html`，经全局 `/ui-f3m8` 挂载；前端库取 `/ui-f3m8/vendor/`） |

API 前缀 `lab-f3m8` 与页面入口 `ui-f3m8` 都用混淆串，降低自动扫描器命中率（无登录）。`/ui-f3m8` 静态资源走网关 normal 档，见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。

所有带 `(task_id, step_id, 可选 run_id)` 的端点都在入口经 `app/routers/utils/runs.py` 解析一次 run，之后只用这个 `RunIdentity`（语义见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)「读侧 run 锁定」）。

## router 与 service 的分工

`app/services/lab/` 包根 `__init__` 只有 docstring、零 re-export，调用方走深路径（`from app.services.lab import service as lab_service`、`runtime_config as lab_config`、`from app.services.lab.types import ClipRange`）。

- **`app/services/lab/service.py`**：无活体，全是模块函数，负责流程。
  - `require_label_studio() -> (url, token)`：任一为空抛 `LabelStudioNotConfiguredError`。
  - `resolve_project_id(requested)`：请求优先（0 / None 视为未传），否则运行时默认值；都没有 → `ValidationError`。
  - `validate_clips(clips) -> List[ClipRange]`：返回按 `start_media_ms` 升序排好的列表。
  - `submit_clips(run, clips, *, project_id, ls_url, ls_token, keep_artifacts_on_failure) -> SubmitOutcome`：不再校验，`clips` 必须是 `validate_clips` 的返回值。
  - `export_step(run, track) -> Path`：产物归调用方删。
  - `ping_label_studio(url, token) -> (bool, Optional[str])`：超时 10s，任何异常折成 `(False, "类型: 消息")`，不抛。
  - ClipBuilder / StepExporter / LabelStudioClient 都在这些函数内按 `settings` 构造；阻塞调用，router 经 `run_in_threadpool` 调，不上事件循环。
- **`app/services/lab/types.py`**：`ClipRange`（NamedTuple，媒体坐标）、`ClipOutcome` / `SubmitOutcome`（dataclass，`ClipOutcome` 字段与 router 的 `LabClipResultDTO` 逐一同名）、单段失败码常量 `ERR_RANGE_OUT_OF_BOUNDS` / `ERR_RANGE_GAP` / `ERR_FFMPEG_FAILED` / `ERR_LS_BAD_RESPONSE`（对外字面量，改名即破坏前端契约）。
- **router（`app/routers/lab.py`）只留**：检查顺序、异常 → HTTP 错误映射、DTO 组装（含 success / failure 计数）、任务列表的查询拼装。「run 有没有 raw 段」的 404 判定留在 router——它依赖 `resolve_run`，而 services 不许 import routers。

## 送标区间用媒体坐标，不收墙钟

`/submit` 的区间字段是 `start_media_ms` / `end_media_ms`（= `<video>.currentTime × 1000`，相对该 run **raw 轨**媒体轴原点）。

**为什么不能收墙钟**：媒体轴是压紧的墙钟，断流那段时间在它上面宽度为零。浏览器手上只有媒体轴上的量，`首段墙钟 + currentTime` 这个换算只在从没断过流时成立——一个含 20s 断流的 run，按它上报会把区间整体推后 Σgap，裁出来的 clip **时长对、能播、不报错，但里面没有操作员标的那个事件**。换算要读清单，只有后端做得了（`hls.MediaTimeline`，见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)「两套坐标」）。

**响应必须带回墙钟**（`start_ms` / `end_ms`，算不出时 null），这不是冗余：媒体刻度是**瞬时坐标、不是持久标识**——它的含义依赖当前段集合，段集合变化会改变前缀和，同一个 `media_ms` 事后就指向另一帧；墙钟由采集那一刻决定，与盘上还剩哪些段无关。**凡是离开本次请求的时刻一律落墙钟**：产物文件名 `clip_{start_ms}_{end_ms}.mp4` 用的就是它。

校验（数量 / 单段时长 / 总时长 / 不重叠）全在媒体轴上做：`validate_clips` 先判数量（`Too many clips`，排序前），再按 `start_media_ms` 升序排好逐段比；校验文案里的 `clip[i]` 是**排序后**下标，不是请求里的原始下标。

## Submit 流程

**503 → 400 → 400 → 404 的检查顺序是对外契约**（`app/routers/lab.py::submit_clips`）：

1. `lab_service.require_label_studio()`：LS url/token 未配置 → 503。
2. `lab_service.resolve_project_id(req.project_id)`：无可用 project_id → 400。
3. `lab_service.validate_clips(...)`：数量 / 单段时长 / 总时长 / 不重叠，上限来自 `settings.lab_export_max_*` → 400。
4. router 里 `resolve_run` + `hls.query_has_segments(run, "raw")`：点名 run 不在 → 404 Run；无可见 run 或无 raw 段 → 404 Segments。
5. `run_in_threadpool(lab_service.submit_clips, run, ordered_clips, ...)`：逐段 ffmpeg 裁剪 + LS 上传（都是阻塞调用），每段构造 `ClipSpec(run, start_media_ms, end_media_ms)`。
6. 单段失败不让整请求失败：HTTP 仍 200，每段在 `clips[]` 里带 `success` / `error_code`（`_process_one` 把 ClipBuilder 的三类异常与 LS 失败折成 `ClipOutcome`；LS 失败时优先透传客户端的 `ls_unreachable` / `ls_auth` 等码）。
7. 全成功或 `keep_artifacts_on_failure=false` → 删整个 job_dir；否则保留并把路径回给调用方。出现非预期异常（非 `ClipBuildError`）时异常冒出、job_dir **不清理**。

## 任务列表数据源（`task_source`：db | storage）

`GET /tasks` 的来源由运行时开关决定（`lab_config.get_task_source()`，持久化在 `lab_runtime_config.json`，默认 `db`）。两模式都只收「有 raw 段的最新可见 run」，都返回 `run_ids: {step_id: run_id}`（前端后续请求带上即锁定同一个 run）与 `offline_steps`（`inference.query_has_offline_results(run)`：`temporal.jsonl` 里有 `TemporalSegment` 或 `label_probs.npz` 在盘上，npz 只判存在不解析）。

- **`db`**：`db_tasks.query_task_page(q, limit=, offset=)` 查 `clean_task`（可按 `source_ip` / `status` / `task_id` 搜索，session 在函数内开关）；逐行再扫盘补 raw 信息，发生在 session 关闭之后。
- **`storage`**：以本地存储目录为列表信息**唯一来源**（`tasks.list_task_ids()` → 逐 task `_list_raw_runs`），不碰业务库也能标注。DB-only 字段无从得知：`source_ip=None`、`status="unknown"`、`current_step`/`step_id` 留空（不推断）；`start_time` / `updated_time` 取各 run `hls.query_span(run, ("raw",))` 的 min(`start_ms`) / max(`end_ms`)（epoch 毫秒），**末端算段尾（段起点 + round(EXTINF)）不是段起点**，否则「最后更新」恒比实际早一个段长。

`_list_raw_runs(task_id)` = `runs.query_latest_by_step(task_id)` 过滤 `hls.query_has_segments(run, "raw")`。⚠ `runs.query_latest_by_step` **只看 run 可见、不看有没有段**，「raw 轨非空才收」由这里补——建了目录没写成段的 step 点开是黑屏。

引入动机：业务后端故障时送标平台不受牵连。开关经 `PUT /config` 的 `task_source` 切换；传 `null` 表示只改其他字段、不冲掉当前模式。

⚠ **已知风险**：`list_lab_tasks` 是 `async def`，但函数体内同步查 DB（db 模式）并逐 task 扫盘（两模式），执行期间阻塞事件循环（同进程的 `/ai/video` WS 等一起卡住）；task 多或盘慢时影响放大。影响程度未实测。

## 逐帧类别概率（`/label-probs`）

`POST /lab-f3m8/label-probs` 是同步 `def` 端点（FastAPI 放线程池跑）：`resolve_timeline(task, step, run_id, track)`（无段 → 404）→ `inference.read_label_probs(run)` → 每帧 `ts`（浮点秒）取 `round(ts × 1000)` 后经 `timeline.media_ms_at` 换成该轨媒体刻度，概率按类转置成一类一条曲线。没有产物返回空数组，不报错。概率只供可视化，不参与任何判断；产物由离线作业写出，见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。admin 页离线 tab 与 lab 共用 `/lab-f3m8/tasks` 清单（取 `offline_steps`）与这条读口。

## 双轨查看（raw / processed）——processed 只看不送标

工作台播放器可在 raw（原始）与 processed（带检测框）两轨间切换：

- **processed 是只读参考轨**：打点按钮（设为起点/终点/加入列表）在 processed 下禁用，送标恒裁 raw。
- **切轨必须重取 timeline**：两轨各自独立切段，Σ EXTINF 与告警 `media_offset_ms` 都不同尺，只换视频源会让标记与画面错位。
- **切 processed 前先 `fetch` 预检其 playlist**（某些 run 只有 raw），`!ok` 则提示并回弹 raw；通过后换源、重取 timeline、seek 回原位并维持播放态。
- **纯前端能力**：全在 `app/static/lab/index.html`。`/submit` 请求体本就无 `track` 字段。

## ClipBuilder

产出 ms 精度 mp4，全程**在清单上做纯减法**（`app/services/lab/clip_builder.py::build_one`）：

1. `hls.query_timeline(spec.run, "raw").select(start_media_ms, end_media_ms)` 取与区间相交的段（区间相交判据：`p.media_start < end AND p.media_end > start`），空 → `ClipRangeOutOfBoundsError`。
2. `first_gap(window)`（`app/services/utils/media_timeline.py` 的模块函数）查真实录制停顿，有 → `ClipRangeGapError`（`error_code=range_gap`）。
3. `window.wall_ms_at()` 把区间两端换成绝对墙钟；区间尾越过轨尾时 `wall_ms_at` 贴到末段段尾，**`duration_ms` 必须跟着收**，否则回给调用方的时长比真实 mp4 长。
4. `window.media_offset_ms(start)` 得 ffmpeg 的 `-ss` 偏移——取子集时 ffmpeg 会把首段 `tfdt` 归一到 0，所以偏移是纯减法，不需要知道整条轨的原点。
5. `render_vod` 拼临时清单 `.clip_{nonce}.m3u8`，落进该 run 的 `hls/` 目录（取 `hls.init_path(run, "raw").parent`），ffmpeg **HLS demuxer** 读 `raw_init.mp4 + fragments`，输出端 libx264 重编码。

三条**静默出错**的硬约束（每条都踩过）：

- **段只从清单来**（`hls.list_segments` / `query_timeline`）。喂未登记的段给 ffmpeg 会 exit 0、无任何日志、产出少一截的 mp4。
- **不用 `-f concat`**：fMP4 fragment 无 moov，单独 demux 解不出 codec init，必须靠 `EXT-X-MAP`。
- **`-ss` 必须在 `-i` 之后**（输出侧 seek）。挪到输入侧 exit 0、产出 261 字节零流空壳。

临时清单的 EXTINF 直接用清单真值，**别改写成「相邻段 ts 差」**——ffmpeg 的时间轴完全来自 fragment 自己的 `tfdt` + sample duration，改写清单 EXTINF 实测是空操作。临时清单名匹配不上域内段名/init 名正则，读侧枚举天然跳过。

## 连续性判据：清单 EXTINF，固定 0.5s 阈值

```
gap = 下一段起点 − (本段起点 + EXTINF)      # 两项都是真值；MediaTimeline.wall_gaps() 逐对产出
真停顿 ⟺ gap > GAP_THRESHOLD_MS (500)      # app/services/utils/media_timeline.py
```

两项都是真值：起点是文件名里的实测墙钟，EXTINF 是清单里声明的段媒体时长。**fps 漂移被天然吸收**——漂移同时压低 `eff_fps` 与段内帧数，`EXTINF = N/eff_fps` 跟着变长。

阈值是**保守护栏而非精确判据**：下界是帧间隔量级的正常残差（典型 ≈0、尾部几十 ms），上界是能触发重连的断流（decoder 退出 → 健康检查 → respawn → RTSP 重连，秒级）。0.5s 落在这一到两个数量级的空当里，且**不随段长或 fps 变**。

**判据必须留在墙钟轴上**：媒体轴是压紧的，相邻段在它上面永远首尾相接，空洞宽度恒为零。

settings 里没有间隙容差类配置项（`lab_export_gap_tolerance_ms`、`default_segment_duration_s` 均不存在）：EXTINF 就是段时长真值，不需要「相邻段 ts 中位差 + 容差」那类补偿。

## 整段导出（StepExporter）——与 ClipBuilder 分工

`GET /download` 走 `lab_service.export_step` → `app/services/lab/step_exporter.py::StepExporter.export(run, track)`，把锁定 run 一轨的**全部已登记段** remux 成单个 mp4 直接下载。两条路径性质不同、**不合并**：

| | `POST /submit`（ClipBuilder） | `GET /download`（StepExporter） |
|---|---|---|
| 范围 | ms 精度媒体区间 | 整个 run 一轨 |
| 编码 | `-ss/-to` + libx264 重编码 | `-c copy` 纯换容器 + `+faststart` |
| 轨 | 恒 raw | `raw` \| `processed`（默认 processed，汇报要带框那轨） |
| 去向 | Label Studio | HTTP attachment 响应 |

> **`-c copy` 不是抄近路，是正解**：段落盘时已由 `app/storage/hls/_fmp4.py` 转成 H.264/yuv420p/CRF23 的 fMP4 fragment，导出只是换容器——磁盘速度、零 CPU、零二次画质损失。跟着 ClipBuilder 一起重编码等于白掉一次画质换零收益。

router 侧：`resolve_run` 为 None 时直接 404（不建 `.lab_exports`）；异常映射 `StepExportNoSegments` → 404、`StepExportInitMissing` → 503、其余 `StepExportError` → 500（前两者是后者子类，先接子类）。

`export()` 的关键约束（与 `ClipBuilder._run_ffmpeg` 同构，坑点相同）：

- **不能用 `-f concat`**、**必须自己补 `#EXT-X-ENDLIST`**（写入侧清单是 LIVE 形态，ffmpeg 会当直播流只读 live edge，前面全丢）、**临时 m3u8 `.export_{nonce}.m3u8` 必须落在该 run 的 `hls/` 目录**（`EXT-X-MAP` 与段名都是相对 URI）——三条同 ClipBuilder。
- **段与时长同走 `hls.list_segments`**：清单即段集合，EXTINF 即时长真值；在途段不在清单里，天然被滤掉。run 仍在录制时下载 = 拿到当前已落盘的部分，不会产出坏文件。
- **缺 init → 503**，与 traceback playlist 同码同措辞：run 目录全由现行代码写出，缺 init 只剩首段仍在 transcode 途中一种可能；无段 / 段全在途 → 404。
- **孤儿回收**：产物落 `{storage_base_dir}/.lab_exports/`（`lab_export_temp_dir` 可覆写），路由挂 `BackgroundTask` 响应发完即删；客户端中途断开时 Starlette 不保证跑到，故 `export()` 开头自扫一遍超 30 min 的 `step_*.mp4` 残留（`_sweep_orphans`）。`.lab_exports` **不在 `CleanupWorker`（`app/daemons/cleanup/worker.py`）扫描范围内**（它只认两级都是十进制数字的 `{task}/{step}/`）——这是**显式依赖**：谁把这个临时目录改成数字名，它就会在 `cleanup_days` 后被当过期 step 整个删掉，而这边不会有任何提示。
- **不做产物缓存**（run 还在录制时段会增长，失效难判）、不限时长体积（成本在磁盘 IO 不在 CPU）。

## 送标 clip 不带元数据

LS 的 `/api/projects/{pid}/import` 在文件上传模式下**只读 `request.FILES`**，同一个 multipart 里的非文件字段一律忽略。`_build_multipart` 是单文件、无附加字段。**素材侧的溯源信息只剩文件名里的墙钟区间**（`clip_{start_ms}_{end_ms}.mp4`）；墙钟仍经 `/submit` 响应回给调用方。

## Label Studio Client

极简 urllib 客户端（`app/services/lab/label_studio_client.py`，沿用 `app/services/alarm/reporter.py` 的 urllib 风格，不引 requests/httpx）：

- `ping()`：`GET /api/version`
- `import_clip()`：`POST /api/projects/{project_id}/import`（multipart 单文件）

multipart 会把整个 mp4 读进内存；Lab 场景 clip 通常 <5 min，可接受。

## 配置

页面可改并持久化（`app/services/lab/runtime_config.py`，文件 `{storage_base_dir}/lab_runtime_config.json`，文件值优先、回退 env）：`label_studio_url`、`default_project_id`、`task_source`。这是运行时状态，不是启动期只读的 `config.py`。

只能经环境变量配置：LS token（恒 = `settings.label_studio_token`，页面不可见、不可改，密钥不经页面流转）。`/health` 由 router 直接读 `lab_config.get_url / get_token / get_default_project_id`（未配置分支要回显这些值），再调 `ping_label_studio`。

裁剪与导出的其余参数全在 `settings`：`lab_export_temp_dir`（空则 `{storage_base_dir}/.lab_exports`）、`lab_export_ffmpeg_preset`、`lab_export_max_clip_ms` / `max_total_ms` / `max_clips_per_submit`。

## 代码来源

- `app/routers/lab.py`
- `app/routers/utils/runs.py`（`resolve_run` / `resolve_timeline`）
- `app/services/lab/service.py`、`app/services/lab/types.py`
- `app/services/lab/clip_builder.py`
- `app/services/lab/step_exporter.py`
- `app/services/lab/label_studio_client.py`
- `app/services/lab/runtime_config.py`（运行时配置与 `task_source`）
- `app/db/tasks.py`（`query_task_page`）
- `app/storage/hls/`（段与 EXTINF 真源、`_timeline.py` 媒体轴、`query_span` / `query_has_segments`）、`app/storage/runs.py`（`query_latest_by_step`）、`app/storage/tasks.py`（task 枚举）、`app/storage/inference/`（`query_has_offline_results` / `read_label_probs`）
- `app/services/utils/media_timeline.py`（`GAP_THRESHOLD_MS` / `first_gap`）
- `app/services/utils/vod_playlist.py`（临时 VOD 清单骨架）
- `app/static/lab/index.html`
- `tests/test_lab_service.py`、`tests/test_lab_router_submit.py`、`tests/test_lab_label_probs.py`、`tests/test_lab_clip_builder.py`、`tests/test_lab_step_exporter.py`、`tests/test_lab_tasks_api.py`、`tests/test_media_timeline.py`（只测断流判据）
