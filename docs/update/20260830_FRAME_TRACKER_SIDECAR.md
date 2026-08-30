# 离线帧回看：段级 sidecar 索引落地

> **变更状态**：生效中（2026-08-30）
> **知识库**：待沉淀
>
> 承接：取代 [20260818_HLS_TIMELINE_INDEX.md](20260818_HLS_TIMELINE_INDEX.md)——单体 tick 索引（`.timeline.idx` / `.timeline.log` / metadata first_ts）已从代码删除，旧文档仅存档。索引链路不再依赖 `_HLS_TIMESCALE=90000`，仅依赖段内 sidecar 索引（无 tick、无 first_ts）。

## 概述

- **写侧**：[hls_strategy `_update_timeline`](../../app/services/persistence/strategies/hls_strategy.py) 每落一个 raw 段，写同名 sidecar `raw_segment_{ts_us}.idx`——该段每帧 `frame.timestamp` 的 float64 原值数组（无 tick、无 first_ts），`tofile(tmp)` + `os.replace` 原子替换。
- **读侧**：[frame_tracker.py](../../app/services/inference/offline/frame_tracker.py) 重写。`Timeline` 用 `SegmentFinder` 按文件名 ts_us 定位段（内存 searchsorted，零文件读），段内读 sidecar `np.searchsorted` 得帧号区间，`concat:raw_init.mp4|seg.mp4` + `select=between(n,k1,k2)` + `-vsync 0` 解码，流式 yield `Frame`。无 m3u8、无 `-ss`、无像素缓存。
- **查询链路**：`ts → SegmentFinder(段) → sidecar(searchsorted, 段内帧号) → concat 解码取帧 → 像素`。

## 改了什么

- 索引与段同名同目录，内容为 float64 ts 原值数组；不依赖 tick / `_HLS_TIMESCALE` / `metadata.json`（原常量两处硬编码、first_ts 依赖、精确查表 KeyError 三个问题随单体索引一并消失）。
- 单体 `.timeline.idx` / `.timeline.log` 已从写侧删除。
- 解码 `concat:{init}|{seg}`、不读 m3u8、`-vsync 0`、无 `-ss`、段内整数帧号；mp4/idx/ts_us 三方命名配对一致（均 `int(start_ts*1e6)`）。
- 无像素缓存，生成器流式输出，常驻峰值 ≈ 1 帧 + 管道缓冲；顺序读每段一次 ffmpeg 调用；尺寸默认 640×480（`scale` 到目标尺寸）。
- 取消索引文件持锁保护，原子写（tmp + `os.replace`）
- ffmpeg 子进程 `-loglevel error` / `-hide_banner` / `stdin=DEVNULL` 已加。
- `FrameTracker.find` 检查解出帧数 ≠ sidecar 条数，若不一致则报错。

## 与方案要求不一致的部分

**方案要求**：最近邻 + 半帧间隔硬阈值，超限报错。 
**现状**：`Timeline` 不负责配对，仅负责按 ts 顺序迭代帧。容差检查由 `FrameTracker.find` 实现。写方和读方同源，精度源头一致。`FrameTracker.find` 固定 `1e-4`（100µs），配对逻辑为单向严格推进。原始校验要求转为校验传入的所有 ts 是否都有对应的 Frame，无则报错。

**方案要求**：ffmpeg 子进程 timeout（clip_builder、step_exporter 三样都有）。 
**现状**：ffmpeg 子进程通过 yield `Frame` 流式输出，未做 timeout。
