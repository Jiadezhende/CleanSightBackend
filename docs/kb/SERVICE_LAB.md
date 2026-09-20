> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Lab Service

Lab 服务从 raw HLS 段裁剪样本视频并提交到 Label Studio 创建标注任务；另有一条不经 LS 的**整段导出**旁路，把某轨已落盘的全部段 remux 成单个 mp4 供下载。

> 端点的请求/响应 schema、字段语义与错误码见 [docs/api/lab.md](../api/lab.md)。本文件只写路由归属、内部分工与能力边界。

## 路由

| 端点 | 归属 |
|------|------|
| `GET /lab-f3m8/tasks` | 可标注任务列表（数据源由运行时 `task_source` 开关决定） |
| `POST /lab-f3m8/submit` | 区间裁剪 + 送标（ClipBuilder → LabelStudioClient） |
| `GET /lab-f3m8/download` | 整段导出下载（StepExporter） |
| `GET /lab-f3m8/health` | LS 配置 + 可达性探测 |
| `GET\|PUT /lab-f3m8/config` | LS 连接配置读写（持久化） |
| `/lab-f3m8/ui` | `StaticFiles` 挂载的送标工作台 |

路径用 `lab-f3m8` 是为降低自动扫描器命中率。

## 送标区间用媒体坐标，不收墙钟

`/submit` 的区间字段是 `start_media_ms` / `end_media_ms`（= `<video>.currentTime × 1000`，相对该 step **raw 轨**媒体轴原点）。

**为什么不能收墙钟**：媒体轴是压紧的墙钟，断流那段时间在它上面宽度为零。浏览器手上只有媒体轴上的量，`首段墙钟 + currentTime` 这个换算只在从没断过流时成立——一个含 20s 断流的 step，按它上报会把区间整体推后 Σgap，裁出来的 clip **时长对、能播、不报错，但里面没有操作员标的那个事件**。换算要读清单，只有后端做得了（`MediaTimeline`，见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)「两套坐标」）。

**响应必须带回墙钟**（`start_ms` / `end_ms`，算不出时 null），这不是冗余：媒体刻度是**瞬时坐标、不是持久标识**——它的含义依赖当前段集合，孤儿段补登记或过期段清理都会改变前缀和，同一个 `media_ms` 事后就指向另一帧；墙钟由采集那一刻决定，与盘上还剩哪些段无关。**凡是离开本次请求的时刻一律落墙钟**：产物文件名 `clip_{start_ms}_{end_ms}.mp4` 用的就是它。

校验（数量 / 单段时长 / 总时长 / 不重叠）全在媒体轴上做，`_validate_clips` 先按 `start_media_ms` 升序排再逐段比。

## Submit 流程

1. 校验 LS url/token 已配置（缺 → 503）。
2. 解析 `project_id`：请求优先，其次运行时配置的默认值，都没有 → 400。
3. 校验 clips（数量 / 单段时长 / 总时长 / 不重叠，上限来自 `settings.lab_export_max_*`）。
4. `hls.list_segments(task_id, step_id, "raw")` 判该 step 有无 raw 段（无 → 404）。
5. 线程池里同步跑 ffmpeg 裁剪 + LS 上传（两者都是阻塞调用）。
6. 单段失败不让整请求失败：HTTP 仍 200，每段在 `clips[]` 里带 `success` / `error_code`。
7. 全成功或 `keep_artifacts_on_failure=false` → 删整个 job_dir；否则保留并把路径回给调用方。

## 任务列表数据源（`task_source`：db | storage）

`GET /tasks` 的来源由运行时开关决定（`lab_config.get_task_source()`，持久化在 `lab_runtime_config.json`，默认 `db`）：

- **`db`**：查 `clean_task` 表，可按 `source_ip` / `status` / `task_id` 搜索，带 status、current_step 等 DB 字段；raw 段信息仍从文件系统补。
- **`storage`**：以本地存储目录为列表信息**唯一来源**（`tasks.list_task_ids()` + `tasks.list_step_ids()` + `hls.list_segments`），不碰业务库也能标注。DB-only 字段无从得知：`source_ip=None`、`status="unknown"`、`current_step`/`step_id` 留空（不推断）；`updated_time` / `start_time` 由段 ts 推导，**末端算段尾（`ts + EXTINF`）不是段起点**，否则「最后更新」恒比实际早一个段长。

引入动机：业务后端故障时送标平台不受牵连。开关经 `PUT /config` 的 `task_source` 切换；传 `null` 表示只改其他字段、不冲掉当前模式。

⚠ `tasks.list_step_ids` **不过滤空 step**（有无产物是域知识，不在目录层；TTL 要的正是没过滤的那档）。送标清单的「raw 轨非空才收」由 `_list_raw_steps` 自己补——建了目录没写成段的 step 点开是黑屏。

## 双轨查看（raw / processed）——processed 只看不送标

工作台播放器可在 raw（原始）与 processed（带检测框）两轨间切换：

- **processed 是只读参考轨**：打点按钮（设为起点/终点/加入列表）在 processed 下禁用，送标恒裁 raw。
- **切轨必须重取 timeline**：两轨各自独立切段，Σ EXTINF 与告警 `media_offset_ms` 都不同尺，只换视频源会让标记与画面错位。
- **切 processed 前先 `fetch` 预检其 playlist**（某些 step 只有 raw），`!ok` 则提示并回弹 raw；通过后换源、重取 timeline、seek 回原位并维持播放态。
- **纯前端能力**：全在 `app/static/lab/index.html`。后端零改动——双轨 playlist 早已支持，`/submit` 请求体本就无 `track` 字段。

## ClipBuilder

产出 ms 精度 mp4，全程**在清单上做纯减法**：

1. `MediaTimeline.load(task_id, step_id, "raw").select(start_media_ms, end_media_ms)` 取与区间相交的段（区间相交判据：`p.media_start < end AND p.media_end > start`），空 → `ClipRangeOutOfBoundsError`。
2. `window.first_gap()` 查真实录制停顿，有 → `ClipRangeGapError`（`error_code=range_gap`）。
3. `window.wall_ms_at()` 把区间两端换成绝对墙钟；区间尾越过轨尾时 `wall_ms_at` 贴到末段段尾，**`duration_ms` 必须跟着收**，否则回给调用方的时长比真实 mp4 长。
4. `window.media_offset_ms(start)` 得 ffmpeg 的 `-ss` 偏移——取子集时 ffmpeg 会把首段 `tfdt` 归一到 0，所以偏移是纯减法，不需要知道整条轨的原点。
5. `render_vod` 拼临时清单落进 `{step}/hls/`，ffmpeg **HLS demuxer** 读 `raw_init.mp4 + fragments`，输出端 libx264 重编码。

三条**静默出错**的硬约束（每条都踩过）：

- **段只从清单来**（`hls.list_segments`）。喂未登记的段给 ffmpeg 会 exit 0、无任何日志、产出少一截的 mp4。
- **不用 `-f concat`**：fMP4 fragment 无 moov，单独 demux 解不出 codec init，必须靠 `EXT-X-MAP`。
- **`-ss` 必须在 `-i` 之后**（输出侧 seek）。挪到输入侧 exit 0、产出 261 字节零流空壳。

临时清单的 EXTINF 直接用清单真值，**别改写成「相邻段 ts 差」**——ffmpeg 的时间轴完全来自 fragment 自己的 `tfdt` + sample duration，改写清单 EXTINF 实测是空操作。临时清单命名 `.clip_{nonce}.m3u8`，匹配不上域内段名/init 名正则，读侧枚举天然跳过。

## 连续性判据：清单 EXTINF，固定 0.5s 阈值

```
gap = 下一段起点 − (本段起点 + EXTINF)      # 两项都是真值
真停顿 ⟺ gap > GAP_THRESHOLD_MS (500)      # media_timeline.py
```

两项都是真值：起点是文件名里的实测墙钟，EXTINF 是清单里声明的段媒体时长。**fps 漂移被天然吸收**——漂移同时压低 `eff_fps` 与段内帧数，`EXTINF = N/eff_fps` 跟着变长。

阈值是**保守护栏而非精确判据**：下界是帧间隔量级的正常残差（典型 ≈0、尾部几十 ms），上界是能触发重连的断流（decoder 退出 → 健康检查 → respawn → RTSP 重连，秒级）。0.5s 落在这一到两个数量级的空当里，且**不随段长或 fps 变**。

**判据必须留在墙钟轴上**：媒体轴是压紧的，相邻段在它上面永远首尾相接，空洞宽度恒为零。

> 被替换掉的旧判据是「该 step 全量段相邻 `ts_us` 间隔的中位数 + 可配容差」——那是**没有段时长真值**时的补偿。EXTINF 就是真值，补偿连同配置项 `lab_export_gap_tolerance_ms`、`default_segment_duration_s` 一并退役，settings 里已无这两项。

## 整段导出（StepExporter）——与 ClipBuilder 分工

`GET /download` 走 `app/services/lab/step_exporter.py`，把 `(task_id, step_id, track)` 的**全部已登记段** remux 成单个 mp4 直接下载。两条路径性质不同、**不合并**：

| | `POST /submit`（ClipBuilder） | `GET /download`（StepExporter） |
|---|---|---|
| 范围 | ms 精度媒体区间 | 整个 step 一轨 |
| 编码 | `-ss/-to` + libx264 重编码 | `-c copy` 纯换容器 + `+faststart` |
| 轨 | 恒 raw | `raw` \| `processed`（默认 processed，汇报要带框那轨） |
| 去向 | Label Studio | HTTP attachment 响应 |

> **`-c copy` 不是抄近路，是正解**：段落盘时已由 `app/storage/hls/_fmp4.py` 转成 H.264/yuv420p/CRF23 的 fMP4 fragment，导出只是换容器——磁盘速度、零 CPU、零二次画质损失。跟着 ClipBuilder 一起重编码等于白掉一次画质换零收益。

`export()` 的关键约束（与 `ClipBuilder._run_ffmpeg` 同构，坑点相同）：

- **不能用 `-f concat`**、**必须自己补 `#EXT-X-ENDLIST`**（写入侧清单是 LIVE 形态，ffmpeg 会当直播流只读 live edge，前面全丢）、**临时 m3u8 必须落在 `{step}/hls/`**（`EXT-X-MAP` 与段名都是相对 URI）——三条同 ClipBuilder。
- **段与时长同走 `hls.list_segments`**：清单即段集合，EXTINF 即时长真值；在途段（mp4v 已落、transcode+append 未完成）不在清单里，天然被滤掉。step 仍在录制时下载 = 拿到当前已落盘的部分，不会产出坏文件。
- **缺 init → 503**，与 traceback playlist 同码同措辞（同一根因，服务端无法自愈）；无段 / 段全在途 → 404。
- **孤儿回收**：产物落 `{storage_base_dir}/.lab_exports/`，路由挂 `BackgroundTask` 响应发完即删；客户端中途断开时 Starlette 不保证跑到，故 `export()` 开头自扫一遍超 30 min 的 `step_*.mp4` 残留。`.lab_exports` **不在 `StorageCleanupWorker` 扫描范围内**（它只认两级都是十进制数字的 `{task}/{step}/`）——这是**显式依赖**：谁把这个临时目录改成数字名，它就会在 `cleanup_days` 后被当过期 step 整个删掉，而这边不会有任何提示。
- **不做产物缓存**（step 还在录制时段会增长，失效难判）、不限时长体积（成本在磁盘 IO 不在 CPU）。

## 送标 clip 不带元数据

LS 的 `/api/projects/{pid}/import` 在文件上传模式下**只读 `request.FILES`**，同一个 multipart 里的非文件字段一律忽略——后端此前随 clip 一起拼的 `label` 等元数据 LS 从来就没收到过。相关字段已从提交链路删除，`_build_multipart` 现在是单文件、无附加字段。**素材侧的溯源信息只剩文件名里的墙钟区间**（`clip_{start_ms}_{end_ms}.mp4`）；墙钟仍经 `/submit` 响应回给调用方。

## Label Studio Client

极简 urllib 客户端（沿用 `alarm_strategy` 的 urllib 风格，不引 requests/httpx）：

- `ping()`：`GET /api/version`
- `import_clip()`：`POST /api/projects/{project_id}/import`（multipart 单文件）

multipart 会把整个 mp4 读进内存；Lab 场景 clip 通常 <5 min，可接受。

## 配置

页面可改并持久化（`{storage_base_dir}/lab_runtime_config.json`，文件值优先、回退 env）：`label_studio_url`、`default_project_id`、`task_source`。

只能经环境变量配置：LS token（恒 = `settings.label_studio_token`，页面不可见、不可改，密钥不经页面流转）。

裁剪与导出的其余参数全在 `settings`：`lab_export_temp_dir`（空则 `{storage_base_dir}/.lab_exports`）、`lab_export_ffmpeg_preset`、`lab_export_max_clip_ms` / `max_total_ms` / `max_clips_per_submit`。

## 代码来源

- `app/routers/lab.py`
- `app/services/lab/clip_builder.py`
- `app/services/lab/step_exporter.py`
- `app/services/lab/label_studio_client.py`
- `app/services/lab/config.py`（运行时配置与 `task_source`）
- `app/services/utils/media_timeline.py`（媒体轴换算、`GAP_THRESHOLD_MS`）
- `app/services/utils/vod_playlist.py`（临时 VOD 清单骨架）
- `app/storage/hls/`（段与 EXTINF 真源）、`app/storage/tasks.py`（step 枚举）
- `app/static/lab/index.html`
- `tests/test_lab_clip_builder.py`、`tests/test_lab_step_exporter.py`、`tests/test_lab_tasks_api.py`、`tests/test_media_timeline.py`
