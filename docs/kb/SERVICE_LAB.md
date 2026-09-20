> 更新时间：2026-09-02
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Lab Service

Lab 服务用于从 raw HLS 段中裁剪样本视频，并提交到 Label Studio 创建标注任务；另有一条不经 LS 的
**整段导出**旁路，把某轨已落盘的全部段 remux 成单个 mp4 供下载。

## 路由

- `GET /lab-f3m8/tasks`：可标注任务列表（数据源由运行时 `task_source` 开关决定，见下）
- `POST /lab-f3m8/submit`
- `GET /lab-f3m8/download`：整段导出下载（`task_id`/`step_id`/`track`，见「整段导出」）
- `GET /lab-f3m8/health`
- `GET /lab-f3m8/config`
- `PUT /lab-f3m8/config`
- 静态 UI：`/lab-f3m8/ui`

路径使用 `lab-f3m8`，代码注释说明目的是降低自动扫描器命中率。

## Submit 流程

1. 校验 Label Studio URL/token 配置。
2. 解析 project_id，请求优先，其次使用默认配置。
3. 校验 clips 数量、单段时长、总时长和不重叠。
4. 检查该 task/step 是否存在 raw segments。
5. 在线程池中同步执行 ffmpeg 裁剪和 Label Studio 上传。
6. 单段失败不让整请求失败，响应中逐段标记 success/error_code。

## 任务列表数据源（task_source：db | storage）

`GET /lab-f3m8/tasks` 的来源由运行时开关 `task_source` 决定（`runtime_config.get_task_source()`，
持久化在 `lab_runtime_config.json`，默认 `db`）：

- **`db`**：走业务数据库（`DBTask`），可按 `source_ip` 搜索、带 status/current_step 等 DB 字段。
- **`storage`（本地数据源）**：以本地 data 目录（`get_default_base_dir()`）为列表信息**唯一来源**，
  扫 `(task_id, step_id)` 目录聚合任务，不查业务数据库也能标注。DB-only 字段无从得知：
  `source_ip=None`、`status="unknown"`、current_step 留空（`_storage_task_to_item`，lab.py:196）。

引入动机：业务后端故障时送标平台不受牵连（切到 storage 即可继续标注）。开关经 `PUT /config` 的
`task_source` 字段切换；`task_source=None` 表示只改其他字段时保持当前模式不被冲掉。

## 双轨查看（raw / processed）——processed 只看不送标

送标工作台播放器可在 raw（原始）与 processed（带模型标注）两轨间切换查看：

- **processed 为只读参考轨**：打点按钮（设为起点/终点/加入列表）在 processed 下禁用，送标恒裁 raw。
- **原因**：打点数学 `currentAbsMs = timeline.start_ms + currentTime` 基于 raw 时基，且 submit 恒裁 raw；
  processed 首段可能因推理预热比 raw 晚起，在其上打点会引入几百 ms 选区偏移。
- **切轨**：切 processed 前先 `fetch` 预检其 playlist，`!ok`（某些 step 只有 raw）则提示并回弹 raw；
  通过后换源并 seek 回原位、维持播放态。
- **纯前端**：全部在 `app/static/lab/index.html`。后端零改动——双轨 playlist
  （`/traceback/task/{id}/playlist.m3u8?track=raw|processed`）早已支持、`submit` 请求体本就无 `track` 恒裁 raw。

## ClipBuilder

ClipBuilder 使用 raw 轨：

1. 通过 SegmentFinder 找到与 `[start_ms, end_ms]` 重叠的 raw 段。
2. 校验相邻段间隙（连续性判据见下）。
3. 构造临时 HLS m3u8，让 ffmpeg HLS demuxer 读取 `{track}_init.mp4 + fragments`（送标只吃 raw 轨，故用 `raw_init.mp4`）。
4. 输出端重编码 libx264，获得 ms 级裁剪 mp4。

代码明确说明不使用 concat demuxer，因为 fMP4 fragment 单独 demux 时缺 codec init。

## 连续性判据（按 step 实测节奏，非固定 10s）

`_validate_continuity`（clip_builder.py:258）判断相邻 raw 段是否存在真实录制停顿。基准**不是**固定的
段时长（切段按固定帧数 `ca_segment_len`，EXTINF = 帧数 / 该段实测 `eff_fps`；而文件名 `ts_us` 是墙钟），
而是**该 step 全量段相邻 `ts_us` 间隔的中位数**（实测节奏）：

```
baseline = median(相邻段 ts_us 间隔)
excess   = 相邻间隔 - baseline
if excess > gap_tolerance_ms: 判真停顿 → 拒裁（error_code range_gap，单片失败）
```

- 系统性 fps 漂移（真实采集达不到 30fps，每段墙钟 >10s，一帧没丢）→ `excess≈0` → 全通过。
- 偶发某段变慢 → `excess` 小 → 通过。
- 真停顿（RTSP 断流/重连级，间隔比基准大数秒）→ `excess` 大 → 仍拒，保留 `range_gap` 语义。

容差 `lab_export_gap_tolerance_ms`（settings.py，默认 **2000ms**，可运维调，`/submit` 透传）。此前按
「假定 10s + 0.5s 容差」比对，**送标片越长越必然踩中**，误报 `range_gap`；本判据修复该长片误报。

> 该判据本身与 EXTINF 无关，仍成立；但 `_validate_continuity` 的 docstring 还写着旧的「EXTINF 恒
> 10.000s（按 `raw_fps=30` 推）」假设——raw 段早已改成按实测 `_effective_fps` 编码（见
> [SERVICE_PERSISTENCE.md](SERVICE_PERSISTENCE.md)），那句注释是陈旧措辞，不影响逻辑。
>
> 遗留项（update/20260614_LAB_CLIP_TIME_MODEL.md 记的「媒体轴与墙钟分歧」）：**系统性分歧的根因已随
> 逐段实测 fps 编码消除**。ClipBuilder 侧仍有两处未收口：段尾用 `_est_segment_duration_us`（相邻
> `ts_us` 中位差）估算而非读 EXTINF；且**不过滤在途段**——回放与整段导出都有这道闸，只有送标没有，
> 实测吃进未转码的裸 mp4v 段不报错、产出残片直接进 Label Studio（既有洞，静默产错标注素材）。

## 整段导出（StepExporter）——与 ClipBuilder 分工

`GET /lab-f3m8/download` 走 `app/services/lab/step_exporter.py`，把 `(task_id, step_id, track)` 的
**全部已落盘段** remux 成单个 mp4 直接下载（取汇报素材 / 原片）。两条导出路径性质不同、**不合并**：

| | `POST /submit`（ClipBuilder） | `GET /download`（StepExporter） |
|---|---|---|
| 范围 | ms 精度区间 | 整个 step 一轨 |
| 编码 | `-ss/-to` + libx264 重编码 | `-c copy` 纯换容器 + `+faststart` |
| 轨 | 恒 raw | `raw` \| `processed`（默认 processed，汇报要带框那轨） |
| 去向 | Label Studio | HTTP attachment 响应 |

> **`-c copy` 不是抄近路，是正解**：段落盘时已由 `hls_strategy` 转成 H.264/yuv420p/CRF23，导出只是
> 换容器——磁盘速度、零 CPU、零二次画质损失。跟着 ClipBuilder 一起重编码等于白掉一次画质换零收益。

`export()` 的关键约束（与 `ClipBuilder._run_ffmpeg` 同构，坑点相同）：

- **不能用 `-f concat`**：段是 fMP4 fragment（无 moov），concat demuxer 单独 demux 找不到 codec init。
  必须走 HLS demuxer，靠 `#EXT-X-MAP` 先吃 `{track}_init.mp4` 再串 fragment。
- **必须自己补 `#EXT-X-ENDLIST`**：写入侧 playlist 是 LIVE 形态，ffmpeg 会当直播流只从 live edge
  读末尾几段，前面全丢。
- **临时 m3u8 必须落在 step 目录**：`EXT-X-MAP` 与段名都是相对 URI，放别处解析不到。
- **时长与在途段判据同走 `parse_playlist_durations()`**：EXTINF 是段时长唯一真值（不能用文件名
  `ts_us` 差重推），其键集合即「已完成 transcode+append 的段」，不在其中的是在途段必须过滤。
- **缺 init → 503**，与 traceback playlist 同码同措辞（同一根因，服务端无法自愈，见
  [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)）；无段 / 段全在途 → 404。
- **孤儿回收**：产物落 `{base_dir}/.lab_exports/`，路由挂 `BackgroundTask` 响应发完即删；但客户端中途
  断开时 Starlette 不保证跑到，且 `.lab_exports` 不在 `StorageCleanupWorker` 扫描范围内（它按
  `metadata.json` 判 step 目录，非数字目录名被跳过），故 `export()` 开头自扫一遍超 30 min 的残留。
- **不做产物缓存**（step 还在录制时段会增长，失效难判）、不限时长体积（成本在磁盘 IO 不在 CPU）。

## Label Studio Client

当前实现是极简 urllib 客户端：

- `ping()`：GET `/api/version`
- `import_clip()`：POST `/api/projects/{project_id}/import`

multipart 会把整个 mp4 读入内存。注释说明 Lab 场景下 clip 通常小于 5 分钟，可以接受。

## 配置

可在页面持久化（`lab_runtime_config.json`，与 HLS 段同一 base_dir，文件值优先、回退 env）：

- Label Studio URL
- 默认 project_id
- `task_source`（`db` | `storage`）

只能通过环境变量配置：

- Label Studio token（恒 = `settings.label_studio_token`，页面不可见、不可改，密钥不经页面流转）

## 代码来源

- `app/routers/lab.py`
- `app/services/lab/clip_builder.py`
- `app/services/lab/step_exporter.py`（整段导出）
- `app/services/lab/label_studio_client.py`
- `app/services/lab/runtime_config.py`
- `app/services/traceback/segment_finder.py`（`parse_playlist_durations` 时长/在途段真源）
- `app/static/lab/index.html`
- `tests/test_lab_clip_builder.py`、`tests/test_lab_step_exporter.py`

