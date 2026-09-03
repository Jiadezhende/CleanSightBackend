# HLS Timeline 索引：ts → frame_num 反查 + ffmpeg 分块提取

> **变更状态**：已废除（2026-08-18）——单体 tick 索引方案已删除，见 [20260830_FRAME_TRACKER_SIDECAR.md](20260830_FRAME_TRACKER_SIDECAR.md)
> **知识库**：无需沉淀（方案已废除，被 [20260830_FRAME_TRACKER_SIDECAR.md](20260830_FRAME_TRACKER_SIDECAR.md) 的段级 sidecar 取代；KB 只记链末状态，不记已死方案。核验过 KB 未被本方案污染——`.timeline.idx`/`.timeline.log` 仅在 [kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md) 的「已废弃残留产物」清单里出现，属有意保留）(2026-09-02)

## 概述

- **改了什么**：raw 段落盘时同步写 `.timeline.idx`（二进制 numpy structured array，每帧一条 tick）+ `.timeline.log`（文本日志）；新增 [frame_tracker.py](../../app/services/inference/offline/frame_tracker.py) 读索引建 tick → frame_num 字典，用 ffmpeg `select=between(n)` 从 raw_playlist 按全局帧号分块提取帧。
- **为什么**：离线推理需要按 records 的 ts 回看 HLS 落盘的原始帧。直接 seek fmp4 文件实现较为复杂，改用 tick → frame_num 索引反查，再调用 ffmpeg 按全局 frame_num 从 raw_playlist 切分块提取帧。
- **影响面**：[hls_strategy.py `_update_timeline`](../../app/services/persistence/strategies/hls_strategy.py)（新增方法）、[frame_tracker.py](../../app/services/inference/offline/frame_tracker.py)（新文件）。

## 原理

### 索引的一致性锚点：tick

两个组件通过同一个 tick 公式对齐：

```
tick = int((frame.ts - first_ts) * _HLS_TIMESCALE)
```
其中，`first_ts` 记录在 `metadata.json` 中，保持不变，作为 tick 基准。
`_HLS_TIMESCALE` 作为 hls_strategy 中公共的 timescale，同样保持不变。

- **写**（[_update_timeline](../../app/services/persistence/strategies/hls_strategy.py)）：每帧落盘时算 tick 追加到 `.timeline.idx`，记录序号 = frame_num（0-based 全局递增）。
- **读**（[Timeline.frame_num_at](../../app/services/inference/offline/frame_tracker.py)）：给定 ts 算同一 tick，查字典得 frame_num。

`_HLS_TIMESCALE` 在两个文件中各自定义同值 90000（[hls_strategy.py L54](../../app/services/persistence/strategies/hls_strategy.py)、[frame_tracker.py L13](../../app/services/inference/offline/frame_tracker.py)），与 cb9d473 pin 的 fMP4 `mdhd.timescale` 一致——tick 既是索引键，也是 fMP4 时间轴的单位，三套时间线（EXTINF / tfdt / timeline 索引）同源。

### frame_num 的含义

`.timeline.idx` 是 append-only 的二进制文件，每条记录的序号 = 该帧在整条 raw_playlist 中的全局帧号（从 0 起，跨段连续递增）。`Timeline._frame_nums` 建的是 `tick → 记录序号(frame_num)` 字典。

### FrameCache 的分块策略

[frame_tracker.FrameCache](../../app/services/inference/offline/frame_tracker.py) 按 `block_size=600` 帧分块，用 ffmpeg `select=between(n\,start\,end)` 从 raw_playlist 一次提取整块，LRU 缓存（capacity=10 块 = 6000 帧）。避免逐帧 seek 的 ffmpeg 进程启动开销；连续查询相邻帧时命中缓存。

### 为什么只在 raw 段写索引

`_update_timeline` 只在 `_persist_raw_segment` 调用：
- 离线推理回看只需 raw 帧（processed 帧率不固定、帧号与 raw 不对齐）。
- `FrameCache._extract_frames` 的 ffmpeg select 按 raw_playlist 的全局帧号提取。

## 改动详情

### 1. [hls_strategy.py `_update_timeline`](../../app/services/persistence/strategies/hls_strategy.py)

新增方法，在 `_persist_raw_segment` 持锁块内调用（L533），紧跟 `_update_metadata` 之后：

- 读 `metadata.json` 的 `raw_segments.first_timestamp` 作为 tick 基准。
- 逐帧算 tick，追加到 `.timeline.idx`（numpy `dtype=[("tick", uint64)]`）和 `.timeline.log`（文本）。
- 持锁保证 append-only 的记录序号与 frame_num 对应关系不被并发段交错破坏。

### 2. [frame_tracker.py](../../app/services/inference/offline/frame_tracker.py)（新文件）

三个类：

- **Timeline**：读 `.timeline.idx` 建 `tick → frame_num` 字典；`frame_num_at(ts)` 算 tick 查表。
- **FrameCache**：LRU + block 分块；`frame_at(ts, w, h)` → `frame_num` → `divmod(block_size)` → 按块提取/缓存 → 取 offset 帧。
- **FrameTracker**：`find(records)` 逐条 ts → `Frame` 的迭代器，离线推理入口。

`_extract_frames` 用 ffmpeg `-vf select=between(n\,start\,end) -vsync 0 -f rawvideo -pix_fmt bgr24 pipe:1`，从 raw_playlist.m3u8 按全局帧号范围提取，输出 rawvideo 到 pipe。
