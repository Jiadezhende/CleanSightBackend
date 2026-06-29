# 送标裁剪连续性判据改为按 step 实测节奏（修长片误报 range_gap）

## 背景：两套时钟不自洽

raw HLS 段录制存在两套时间系统：

| 时钟 | 来源 | 公式 | 性质 |
|------|------|------|------|
| **A. 墙钟** `ts_us`（段文件名） | `frames[0].timestamp = time.time()`（[decoder.py:341](../../app/services/stream/decoder.py#L341)，解码时刻） | 实测墙钟 | 真实物理时间 |
| **B. 媒体时间** EXTINF / fMP4 tfdt | `len(frames) / raw_fps`（[hls_strategy.py:477](../../app/services/persistence/strategies/hls_strategy.py#L477)） | 帧数 ÷ **假定** fps | 推算，非实测 |

关键约束：切段按**固定 300 帧**（`ca_segment_len`，[queues.py:179](../../app/services/client/queues.py#L179)），`raw_fps=30` 是写死常量 ⟹ 每个完整段 **EXTINF 恒 = 300/30 = 10.000s**，与这 300 帧实际花多少墙钟无关。

真实采集达不到 30fps（解码/源跟不上）时：「300 帧实际花了 10.83s 墙钟」= 真实 ~27.7fps，**一帧没丢、内容连续**——这不是录制空洞，是 **fps 漂移 / 瞬时变慢**。

补充：raw 录制路径**正常不丢帧**（[queues.py:161](../../app/services/client/queues.py#L161) 无条件写入，仅持久化严重积压致 deque 溢出才丢），所以墙钟「跳变」基本只来自解码器停顿（源断流/重连）——那才是真正的内容跳变。

## 问题

`ClipBuilder._validate_continuity` 拿墙钟间隔与**假定的 10s**（+0.5s 容差）比，每段都贡献固定表观间隙，**送标片越长越必然踩中**，误报 `range_gap`（error_code `range_gap`，单片失败）。

## 本次改动

`ClipBuilder._validate_continuity`（[clip_builder.py](../../app/services/lab/clip_builder.py)）：连续性基准从「假定 10s」改为**该 step 全量段相邻间隔的中位数**（实测节奏，对偶发停顿稳健）：

```
baseline = median(该 step 所有相邻段 ts_us 间隔)
excess   = 相邻间隔 - baseline
if excess > 容差(lab_export_gap_tolerance_ms, 默认 2000ms): 判真停顿 → 拒裁 range_gap
```

- 系统性 fps 漂移（每段都 >10s）→ `excess≈0` → 全通过（旧逻辑逐段误拒）。
- 偶发某段变慢（10.83 vs 基准 10.1）→ `excess≈0.7s` → 通过。
- 真停顿（间隔比基准大数秒）→ `excess` 大 → 仍拒（保留 `range_gap` 语义）。

配套：[settings.py](../../app/settings.py) 新增 `lab_export_gap_tolerance_ms`（默认 2000ms，可运维调），[lab.py](../../app/routers/lab.py) 透传。容差默认从旧 0.5s 放宽到 2s——旧值在新语义下仍会误拒瞬时变慢，2s 既吸收亚秒~1s 抖动又能拦住 RTSP 重连级（≥2s）的真停顿。

## 已知 / 延后项（本次未修）

1. **播放时间被压缩、绝对时间漂移**：媒体轴（tfdt = Σ EXTINF）按 30fps 重放实际更低帧率内容 → 播放整体偏快，告警 marker 定位随之漂移。前端 `timeline.duration_ms` 用墙钟跨度（[traceback.py:498](../../app/routers/traceback.py#L498)）、`<video>.duration` 用 Σ EXTINF，两者按 fps 偏差分歧。
2. **裁剪偏移漂移**：`_run_ffmpeg` 的 `offset = start_ms*1000 − first_selected_seg.ts_us` 混用墙钟与媒体时间；当选段窗口不从 step 起点开始时，会少算窗口前累计 fps 漂移，使裁剪起点偏移（通常亚秒，系统性漂移/深窗口下更大）。
3. **彻底解法**（需评估）：录制按实测 fps 编码 + EXTINF=实测墙钟时长，使两钟对齐，可一并消除 1、2。风险：[hls_strategy.py:473-476](../../app/services/persistence/strategies/hls_strategy.py#L473-L476) 警告——只改 EXTINF 而 fragment 仍按 30fps 会与 tfdt 不符、引发 hls.js 段尾 MSE 缓冲洞（卡死+总时长缩水），必须连 cv2 编码 fps 一起改，属热路径改动。
