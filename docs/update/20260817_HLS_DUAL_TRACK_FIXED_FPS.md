# HLS 双轨固定帧率：raw / processed 各自 init.mp4 + 编码 fps 不再反推

> **变更状态**：生效中（2026-08-17）
> **知识库**：待沉淀
>
> 承接：[20260722_FPS_TIME_VS_FRAME_DECOUPLE](20260722_FPS_TIME_VS_FRAME_DECOUPLE.md) 的「时间是唯一货币」判据。那次把 raw 段也改成读 `_effective_fps(frames)`，本篇回退为固定 `raw_fps`，并补上漏掉的「双轨 init.mp4」。

## 概述

- **改了什么**：HLS raw / processed 段的编码 fps 不再从帧时间戳反推，分别固定为 `settings.raw_fps` 与 `settings.inference_fps`；同一 step 目录下两条轨各自维护 `raw_init.mp4` / `processed_init.mp4`，不再共用 `init.mp4`。
- **为什么改**：`Frame.timestamp` 取自 [decoder.py `time.time()`](../../app/services/stream/decoder.py)，虽约定当作墙钟、却混入解码抖动与背压等待，直接参与 fps 运算会污染回放速率；更致命的是逐段不同的 fps 会破坏 HLS fMP4 的 timescale 一致性。
- **影响面**：[hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py)、[media.py](../../app/routers/media.py)、[traceback.py](../../app/routers/traceback.py)、[clip_builder.py](../../app/services/lab/clip_builder.py)、[step_exporter.py](../../app/services/lab/step_exporter.py) 及对应测试。


## 动机

### 1. `Frame.timestamp` 不是真墙钟

[decoder.py `_process_frames`](../../app/services/stream/decoder.py) 给每帧盖 `time.time()`——这是 Python 解码线程收 chunk 时的本地墙钟，夹带 ffmpeg RTSP 缓冲、`-vsync drop` 丢帧、背压等待、GIL 调度等噪声。系统约定把它当作墙钟（用于去重、`tfdt` 偏移、段连续性判据足够），但**不应直接参与 fps 反推**：`(N-1)/(ts_last-ts_first)` 把噪声当作源速率，解码线程被抢占多就慢放、丢帧时 ts 间隔没同步缩小就快放。

### 2. 即使 fps 算对了，逐段不同也会让 HLS 卡死

HLS fMP4 的 `EXT-X-MAP`（init.mp4）是整 playlist 共用的「解码器配置 + 时间基准」，里面固化了 `mdhd` 的 **timescale** 与 SPS/PPS。本系统 [_transcode_to_fmp4_segment](../../app/services/persistence/strategies/hls_strategy.py) 只在首段生成 init.mp4、后续段复用——也就是整 playlist 的时间基准被首段钉死。若每段用不同 `eff_fps` 编码：cv2 写出的 mp4v 内部 timescale 与帧间隔各异，ffmpeg 转码到 fMP4 时后续段的时间戳要往首段 timescale 上凑，逐段量化误差累积 → fragment 的 `tfdt`/`trun` duration 与 playlist 声明的 `#EXTINF` 不严格匹配 → hls.js 通过 MSE（Media Source Extensions，浏览器给 `<video>` 流式 append 媒体片段的 API）把 fragment 顺序喂进 SourceBuffer 时，相邻段时间戳不首尾相接 → 出现 buffer gap → 段尾停摆、播放卡死。

**结论**：HLS 编码 fps 必须全 playlist 统一并与 init.mp4 同源，固定值是唯一解。

### 3. raw 与 processed 是两条不同速率的流，必然要两份 init.mp4

raw@30 与 processed@15 各有 playlist，timescale 应分别来自 `raw_init.mp4` / `processed_init.mp4`。旧代码两条 playlist 都写 `#EXT-X-MAP:URI="init.mp4"`，transcode 不分轨——**谁先到谁生成**，第二条轨就指向一个 timescale 不匹配的 init.mp4，直接触发 §2。

## 改动详情

### 1. [hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py)

- `__init__` 增加 `self.raw_fps = settings.raw_fps` / `self.processed_fps = settings.inference_fps`，删除「全程从 ts 反推」的注释。
- `_persist_raw_segment` / `_persist_processed_segment`：`eff_fps` 从 `self._effective_fps(frames)` 改为 `self.raw_fps` / `self.processed_fps`。
- playlist 头与 transcode 调用按轨区分：raw 段写 `raw_init.mp4`、调 `_transcode_to_fmp4_segment(path, "raw")`；processed 段同款。
- `_get_or_cache_timescale(target_dir, segment_type)` / `_transcode_to_fmp4_segment(path, segment_type)`：`init_path` 改 `f"{segment_type}_init.mp4"`；顺带把 ffmpeg `-i` 改 `str(path.resolve())`，消除 cwd 隐式依赖。
- [`_effective_fps`](../../app/services/persistence/strategies/hls_strategy.py) 函数体与上下文常量**保留未删**，已无调用点，留单独清理。

### 2. [media.py](../../app/routers/media.py) — init 路由白名单

token 校验从字面量 `init.mp4` 放宽到「以 `init.mp4` 结尾」，两类 init 都能走该路由取段。

### 3. [traceback.py](../../app/routers/traceback.py) `_build_vod_playlist`

按 `track` 参数构造 init 路径与 token filename，原硬编码 `init.mp4` 替换为 `f"{track}_init.mp4"`。

### 4. [clip_builder.py](../../app/services/lab/clip_builder.py)

打点拼接只用 raw 段，固定指向 `raw_init.mp4`（路径校验 + `#EXT-X-MAP` URI）。

### 5. [step_exporter.py](../../app/services/lab/step_exporter.py)

`_build_vod_text` 加 `track` 参数透传到 `EXT-X-MAP` URI；`_export_step` 先校验 `step_dir / f"{track}_init.mp4"` 存在再拼 m3u8。

### 6. 测试

`test_lab_clip_builder.py`、`test_lab_step_exporter.py`、`test_traceback_router.py` 中所有 `init.mp4` 字面量按上下文替换为 `raw_init.mp4` / `processed_init.mp4`；`_seed_task` 同时 seed 两份 init。

## 未处理：丢帧静默不补偿

ffmpeg `-vsync drop` 与解码侧背压丢帧当前**全部静默接受**，raw 段按固定 `raw_fps` 编码：段内实际帧数 N < `raw_fps × 墙钟时长` → 段媒体时长 = `N/raw_fps` < 真实墙钟，丢帧导致的「时间压缩」直接落到回放（10s 真实段可能只播 8s）。

EXTINF 与 fragment 实际时长仍一致（都用 `N/raw_fps`），**不会触发 §2 的卡顿**，只是回放比真实时间快。`frames_dropped` 与 `frame_drop_total.labels(reason="ingress_backpressure")` 仅做 metrics 计数，满了就 dump、不补帧。

不修的原因：补帧要么黑帧填充，要么改回 wall-clock EXTINF（回到 §2 错位）。正确方向是改 `Frame.timestamp` 来源用 ffmpeg PTS，属解码层改造，不在本篇范围。

