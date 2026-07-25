> 更新时间：2026-07-21
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Lab Service

Lab 服务用于从 raw HLS 段中裁剪样本视频，并提交到 Label Studio 创建标注任务。

## 路由

- `GET /lab-f3m8/tasks`：可标注任务列表（数据源由运行时 `task_source` 开关决定，见下）
- `POST /lab-f3m8/submit`
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
3. 构造临时 HLS m3u8，让 ffmpeg HLS demuxer 读取 `init.mp4 + fragments`。
4. 输出端重编码 libx264，获得 ms 级裁剪 mp4。

代码明确说明不使用 concat demuxer，因为 fMP4 fragment 单独 demux 时缺 codec init。

## 连续性判据（按 step 实测节奏，非固定 10s）

`_validate_continuity`（clip_builder.py:258）判断相邻 raw 段是否存在真实录制停顿。基准**不是**固定的
段时长（EXTINF 因写死 `raw_fps=30` + 固定 300 帧切段恒 = 10.000s），而是**该 step 全量段相邻 `ts_us`
间隔的中位数**（实测节奏）：

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

> 待核验遗留项（本次未修，见 update/20260614_LAB_CLIP_TIME_MODEL.md）：媒体轴（Σ EXTINF，按 30fps）
> 与墙钟跨度分歧 → 播放偏快、告警 marker 漂移；`_run_ffmpeg` 的 offset 混用墙钟与媒体时间，深窗口下
> 裁剪起点亚秒级偏移。彻底解法需录制按实测 fps 编码 + EXTINF=实测墙钟，属热路径改动。

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
- `app/services/lab/label_studio_client.py`
- `app/services/lab/runtime_config.py`
- `app/static/lab/index.html`
- `tests/test_lab_clip_builder.py`

