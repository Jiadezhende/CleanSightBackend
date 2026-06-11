> 更新时间：2026-05-24
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

- cv2 使用固定 fps 写 N 帧。
- 实际媒体时长是 `N / fps`。
- ffmpeg 转码到 fMP4 后保持该媒体时长。
- 若用 wall-clock 帧时间戳差写 EXTINF，可能造成 hls.js 段尾 MSE 缓冲洞、卡死和总时长缩水。

因此 raw 段使用 `len(frames) / raw_fps`，processed 段使用 `len(frames) / processed_fps`。

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

## init.mp4

fMP4 fragment 播放需要 `init.mp4`。VOD playlist 缺少 init 时返回 503，并提示历史段需迁移。

## 代码来源

- `app/services/persistence/strategies/hls_strategy.py`
- `app/routers/traceback.py`
- `app/services/traceback/segment_finder.py`
- `scripts/transcode_segments_to_h264.py`
- `tests/test_traceback_router.py`
- `tests/test_lab_clip_builder.py`

