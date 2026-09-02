> 更新时间：2026-07-25
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# HLS 时间线设计

HLS 追溯的关键约束是：playlist 中的 EXTINF 是播放时长真值，不能用 wall-clock 帧时间戳差替代。

## 写入路径

HLS 策略先用 cv2 写 mp4v，再用 ffmpeg 转为 fMP4 fragment。

段文件命名：

- `raw_segment_{ts_us}.mp4`
- `processed_segment_{ts_us}.mp4`

其中 `ts_us` 来自首帧时间戳，用于排序和定位，不直接作为段时长。

## EXTINF 真值

代码注释明确说明：

- cv2 用某一 fps 写 N 帧，实际媒体时长 = `N / fps`。
- ffmpeg 转码到 fMP4 后保持该媒体时长。
- 若用 wall-clock 帧时间戳差写 EXTINF，可能造成 hls.js 段尾 MSE 缓冲洞、卡死和总时长缩水。

段 fps 由 `_effective_fps(frames)` 求得：`(N-1) / (ts_last - ts_first)`（帧时间戳跨度），**不接收任何上游 fps**；退化段（`span<=0`/单帧/反推值落 `[1,60]` 带外）回落本地常量 `_DEGENERATE_FALLBACK_FPS=15.0`。**同一 fps 同时用于 cv2.VideoWriter 与 EXTINF**，保证 raw/processed 段（经推理/可视化后帧率可能非名义值）的写入帧率与时长一致，不会快放（曾因固定 fps 写入实测更少帧数的段导致 2x 快放）。

## 原子更新

对同一 `{task_id}/{step_id}` 目录，transcode、playlist append 和 metadata update 必须在目录锁内完成。

原因：

- 相邻段 transcode 需要读取 playlist 计算累计时间。
- 并发写入若读到相同累计 EXTINF，可能导致 tfdt 碰撞。

## VOD 过滤在途段

追溯生成 VOD playlist 时，只使用已出现在写入侧 playlist 中的 segment。

如果文件已落地但 playlist 还没有 EXTINF，该段被视为在途段并过滤，避免播放列表估算时长与 fMP4 fragment 不一致。

## Timeline 时长

`/traceback/task/{task_id}/timeline` 计算 step 起止时间时，end 必须取：

```text
max(segment.ts + EXTINF)
```

而不是 `max(segment.ts)`，否则会漏掉最后一段自身时长。

## init 段

fMP4 fragment 播放需要 init 段。**按轨分存 `{track}_init.mp4`**（`raw_init.mp4` /
`processed_init.mp4`）——raw 与 processed 是两条独立 playlist、各有各的 `#EXT-X-MAP`，
共用一个文件名会变成「谁先转码谁定」，另一条轨就指向别人的 init。

缺 init 时回放（VOD playlist）与下载（step 导出）均返回 **503**，且**服务端无法自愈**：
要么段是分轨命名之前的旧格式产物（不支持，也不提供迁移路径——旧落盘结构一律不做兼容，
见 [../update/20260902_LEGACY_LAYOUT_CLEANUP.md](../update/20260902_LEGACY_LAYOUT_CLEANUP.md)），
要么首段仍在 transcode 途中（窗口极短）。

> 本节之后的增量（timescale pin=90000、逐段 sidecar `.idx`）见 `docs/update/`
> 里 20260813 / 20260830 两篇，尚未融合进本文。

## 代码来源

- `app/services/persistence/strategies/hls_strategy.py`
- `app/routers/traceback.py`
- `app/services/traceback/segment_finder.py`
- `tests/test_traceback_router.py`
- `tests/test_lab_clip_builder.py`

