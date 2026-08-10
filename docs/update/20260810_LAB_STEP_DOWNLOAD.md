# 送标面板加整段下载接口：`GET /lab-f3m8/download`，纯 remux 不重编码

> **变更状态**：生效中（2026-08-10）
> **知识库**：待沉淀
>
> 对外契约见 [docs/api/lab.md](../api/lab.md)；HLS 落盘约定见 [kb/DESIGN_HLS_TIMELINE.md](../kb/DESIGN_HLS_TIMELINE.md)。

## 概述

- **改了什么**：新增 `GET /lab-f3m8/download?task_id=&step_id=&track=`，把一个 step 某一轨已落盘的全部段 remux 成单个 mp4 直接下载；送标面板 ② 卡片加了下载按钮。
- **为什么改**：取汇报用的视频识别素材此前只能手工做——rsync 拉段 → 本地补 `#EXT-X-ENDLIST` → ffmpeg HLS demuxer 拼接，三个坑全靠人记（见下）。原有的 `scripts/video_export/`（`concat_and_upload.py` + 操作步骤 README）三条全踩、早已失效，**本次一并删除**。
- **影响面**：新增 1 个路由 + 1 个 service 模块；`_parse_existing_playlist` 从 router 层下沉到 service 层（行为不变）；面板 HTML 加按钮。无新表、无新配置项、无持久化状态。

手工路径的三个坑，正是本接口封装掉的东西：

| 坑 | 后果 |
|----|------|
| 段是 fMP4 fragment（无 moov） | `ffmpeg -f concat` 必失败；须靠 `#EXT-X-MAP` 先吃 `init.mp4` |
| 写入侧 playlist 无 `#EXT-X-ENDLIST` | ffmpeg 当直播流，只从 live edge（末尾几段）读，前面全丢 |
| 目录是 `{task_id}/{step_id}/` | 旧脚本假设 `{client_id}/{task_id}/`，扫不到任何段 |

## 改动详情

### 1. `app/services/traceback/segment_finder.py` — 下沉 playlist 时长解析

`_parse_existing_playlist()` 原本住在 [app/routers/traceback.py](../../app/routers/traceback.py)，但它是纯文件系统解析、无 HTTP 语义。移到 service 层并更名 `parse_playlist_durations()`，[traceback.py](../../app/routers/traceback.py) 两处调用点改 import，行为不变。

否则新增的 service 要反向 import router 才能复用——这正是要避免的跨层依赖。

该函数的返回值同时承担两个职责，两个消费方（traceback VOD playlist / lab 整段导出）都依赖：

- `EXTINF` 是段时长**唯一真值**，不能用文件名 `ts_us` 差重推（重推会与 fragment 实际媒体时长对不上）；
- 键集合即「已完成 transcode+append 的段」，**不在其中的是在途段，必须过滤**。

### 2. `app/services/lab/step_exporter.py`（新增）— 整段导出

```
StepExporter.export(task_id, step_id, track) -> Path
  ├─ 0. sweep temp_root 下超 30 min 的孤儿产物
  ├─ 1. list_segments → 空则 StepExportNoSegments
  ├─ 2. parse_playlist_durations 取真 EXTINF + 过滤在途段 → 空则同上
  ├─ 3. init.mp4 不存在 → StepExportInitMissing
  ├─ 4. 在 step 目录写临时 .export_{nonce}.m3u8（MAP + 真 EXTINF + ENDLIST）
  ├─ 5. ffmpeg -allowed_extensions ALL -i <m3u8> -c copy -movflags +faststart
  └─ 6. finally 删临时 m3u8；产物归调用方
```

关键点：

> **`-c copy` 不是抄近路，是正解**。段落盘时已由 [`_transcode_to_fmp4_segment`](../../app/services/persistence/strategies/hls_strategy.py) 转成 H.264/yuv420p/CRF23，导出只是换容器——磁盘速度、零 CPU、**零二次画质损失**。这里若跟 ClipBuilder 一样重编码，等于白掉一次画质换零收益。

> **临时 m3u8 必须落在 step 目录**：`#EXT-X-MAP:URI="init.mp4"` 和段名都是相对 URI，放别处解析不到。与 [`ClipBuilder._run_ffmpeg`](../../app/services/lab/clip_builder.py) 同一约束。

> **孤儿回收**：客户端中途断开时 Starlette 的 `BackgroundTask` 不保证跑到。`.lab_exports` 又不在 [StorageCleanupWorker](../../app/services/persistence/workers/cleanup_worker.py) 扫描范围内（它按 `metadata.json` 判 step 目录，非数字目录名被 `_dir_name_to_int` 跳过），故在 `export()` 开头自扫一遍。

### 3. `app/routers/lab.py` — 新增 `GET /download`

- `track` 用 `Query(pattern="^(raw|processed)$")`，默认 `processed`（与 traceback playlist 一致；汇报要带框的那一轨）
- ffmpeg 是阻塞调用，走 `run_in_threadpool`（与 `/submit` 同样式）
- 返回 `FileResponse(..., filename=...)` —— `filename=` 让 Starlette 自动带 `Content-Disposition: attachment`，区别于 [media.py](../../app/routers/media.py) 播放用的 `inline`
- `background=BackgroundTask(output_path.unlink, missing_ok=True)` 响应发完删产物
- 异常映射：`StepExportNoSegments`→404（`NotFoundError`）、`StepExportInitMissing`→503（与 traceback 同码同措辞）、`StepExportError`→500

### 4. `app/static/lab/index.html` — 面板按钮

② 卡片 track 单选组右侧加 `<a :href="downloadUrl" download>`，`downloadUrl` 是跟随当前 `track` 的 computed。

> 用原生 `<a download>` 而非 `fetch`+blob：浏览器接管下载进度、可断点续传、**不把整个文件读进内存**。

### 5. 删除 `scripts/video_export/`

`concat_and_upload.py` 与 `README_操作步骤.md` 一并删除。该脚本在当前落盘格式下**必然失败或产出坏视频**（目录假设错 + concat demuxer 读不了 fMP4），且仓库内无任何代码引用它——留着只会误导后人照旧文档操作。功能已被本接口完全覆盖。

需要历史流程时从 git 历史取：`git show <本次提交>^:scripts/video_export/README_操作步骤.md`。

### 6. 保留项（不改动）

- **`ClipBuilder` 保持 raw-only、保持 libx264 重编码**。ms 精度裁剪必须 `-ss/-to`，与整段 remux 是两种性质，不合并、不共用参数。

## 数据通道 / 行为说明

| 通道 | 填充 | 消费 | 本次影响 |
|------|------|------|---------|
| `{base}/{task}/{step}/{track}_segment_*.mp4` | hls_strategy | traceback playlist / ClipBuilder / **StepExporter** | 否（只读） |
| `{track}_playlist.m3u8` 的 EXTINF | hls_strategy | traceback VOD / `_step_duration_ms` / **StepExporter** | 否（只读，解析函数换了位置） |
| `{base}/.lab_exports/` | ClipBuilder job_dir / **StepExporter 产物** | 下载响应 | 是（新增 `step_*.mp4` 命名空间 + 30 min 孤儿回收） |

## 验证

| 项 | 结果 |
|----|------|
| `tests/test_lab_step_exporter.py`（新增） | 13 passed |
| 全量 `pytest tests/` | **420 passed** |
| 真实段导出（3×45 帧 @15fps 经 `HLSPersistenceStrategy` 落盘） | 产物 `duration=9.000000` = 3×3.000s EXTINF；`nb_read_frames=135` = 3×45，无丢帧/重帧 |
| 产物编码 | `codec_name=h264`、320×240、`avg_frame_rate=15/1`，与源一致 |
| faststart | box 顺序 `ftyp, moov, free, mdat` —— moov 前置 |
| 全量解码 | `ffmpeg -i out.mp4 -f null -` 无 error |
| 路由 200 | `Content-Type: video/mp4`、`Content-Disposition: attachment; filename="task1_step1_processed.mp4"`、37942 bytes |
| 路由 404 / 422 / 503 | 未知 task → 404；`track=bogus` → 422；移走 `init.mp4` → 503（措辞与 traceback 一致） |
| `BackgroundTask` 清理 | 下载完成后 `.lab_exports` 为空；step 目录无 `.export_*.m3u8` 残留 |

## 已知取舍

- **不做产物缓存**，每次重新 remux。remux 是磁盘速度，加缓存要处理失效（step 仍在录制时段会增长），不划算。
- **不限时长/体积**。成本在磁盘 IO 不在 CPU；真需要限再按实测加。
- **不做 ms 区间裁剪**。要精确区间走 `/submit` 那条。
