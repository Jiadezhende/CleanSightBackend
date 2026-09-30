# 段查询收口到清单：域里只剩一个段枚举器

> **变更状态**：已实现（2026-09-20）
> **选型依据**：[20260919_VIDEO_TIMEBASE_SELECTION.md](20260919_VIDEO_TIMEBASE_SELECTION.md) Q3（该文 §6 有编号 → 章节对照表）
> **知识库**：已沉淀 → [DESIGN_HLS_TIMELINE.md](../kb/DESIGN_HLS_TIMELINE.md)、[ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)（2026-09-20）
>
> 这是媒体轴那批改动的第一步，可独立落地、独立回滚。送标与进度条改用媒体坐标
> （同一批选型的 Q1 / Q5）是下一步，不在本文。

## 修的是什么

**「有哪些段」过去有两个真源**：文件系统枚举（`iterdir` + 文件名正则）与清单。前者把**在途段**
（mp4v 已落、transcode+append 未完成）和**登记失败的段**（段已就位、`_m3u8.append` 抛了
`OSError`）一并算进来。

喂给 ffmpeg 的后果是**测试全绿、日志无输出、产物看起来正常**：exit 0、任何日志级别都无输出、
`-xerror` 抓不住，产出结构完整、ffprobe 满意、能播、但**少一截**的 mp4——直接进标注流水线
（见 [`kb/DESIGN_SEGMENT_CONCAT.md`](../kb/DESIGN_SEGMENT_CONCAT.md) §5.3）。

## 1. 改了什么

| 文件 | 改动 |
|---|---|
| [`hls/_m3u8.py`](../../app/storage/hls/_m3u8.py) | `durations()` → `entries()`：**有序** `(段名, EXTINF)` 列表 |
| [`hls/_read.py`](../../app/storage/hls/_read.py) | `list_segments` 改纯清单解析；`list_segments_in_range` 同源、同返回类型 |
| [`hls/_layout.py`](../../app/storage/hls/_layout.py) | **原 `list_segments` / `list_segments_by_track` 整个删除** |
| [`hls/types.py`](../../app/storage/hls/types.py) | `PlayableSegment` → `Segment` |
| [`hls/__init__.py`](../../app/storage/hls/__init__.py) | 出口收口到一个枚举器 |
| 调用点 | `lab.py` ×3、`task.py` ×1、`traceback.py` ×1、`step_exporter.py` ×1、`clip_builder.py` ×2、`_decode.py` ×1 |

**不是降级为私有，是整个删除。** 初稿计划把文件系统枚举改名成 `_scan_segment_files` 留给
"删除与自检"，落地时发现**零调用点**——`_write.delete` 走 `rmtree`，TTL 回收看目录 mtime，
都不需要枚举。留一个零调用的私有枚举器就是留着第二个真源，而 `__all__` 拦得住包外、拦不住
包内。日后做「孤儿段补登记」自检工具时再按需重写（那是自检的事，不是查询的事）。

## 2. 名字与类型跟着收口：`list_segments` / `Segment`

收口后域里**不存在"不可播的段"这一类**，所以"可播"不再是限定词：

```text
list_playable_segments  →  list_segments          （原同名的文件系统枚举器已删除，名字腾出来了）
PlayableSegment         →  Segment
list_segments_in_range  →  返回类型跟着变成 List[Segment]（原来只给 SegmentRef）
```

两个枚举器同名族、同返回类型，下游不必按"调用的是哪一个"来分支。

**代价**：`list_segments` 这个名字被留用，所以"漏改的调用点会 `AttributeError`"这条保护只
剩一半——按元素取值的老代码仍会响（`SegmentRef.ts_us` 在 `Segment` 上不存在），但**只做真值
判断的**（`if hls.list_segments(...)`）会静默换掉判据。仓库内这样的点只有 3 处且都是本来就要
换的；风险面是别的分支上 cherry-pick 回来的代码。

**门禁跟着换了判据**：`tests/test_storage_hls.py::TestNoFilesystemEnumerator` 原本断言
"`list_segments` 这个名字不存在"，现在改为断言 `hls.list_segments is _read.list_segments`
——`_layout` 重新长出同名函数时它会红。

## 3. 两处顺带的行为变更

**`traceback.py` 的 404/503 先后对调。** 段检查必须在 init 检查之前，否则不存在的 task/step
会先撞上"缺 init"而得到 503——那是"服务端暂时不可用、请重试"的语义，对一个不存在的资源是
误导。两档 404 文案合一（"挑错 track / 首段仍在转码"），`step_exporter` 同。

404 的**响应体形态**要保持结构化（`NotFoundError` → 全局处理器）。塌成一档时若图省事改用裸
`HTTPException`，body 会静默从结构化变成只有 `detail`，而状态码没变——只断言 404 的用例发现
不了。已补一条钉 body 形态的用例。

**`lab.py` 的 `updated_time` 改取 `max(ts + EXTINF)`**，原先取 `max(ts)` 会恒比实际早一个
段长（~10s）。

## 4. `clip_builder` 只改了取数，没改时间模型

它现在从 `list_segments` 取身份键，但**仍只用 ts、不用 EXTINF**：`-ss` 的 seek 基准还是文件名
里的墙钟 ts，每段时长仍取相邻段 ts 差，连续性判据仍是「中位数基准 + 可配容差」。

本次只修掉"吃进未登记段"那条。墙钟/媒体两套刻度混用的那条（送标区间早 Σgap）归下一步，届时
本文件整个重写、`lab_export_gap_tolerance_ms` 退役。

## 5. 已知代价

| 项 | 影响 | 为什么接受 |
|---|---|---|
| 未登记段的帧对离线反查不可达 | `features.jsonl` 里有特征但取不到帧，`FrameTracker.find()` 报找不到 | 成因只有 `append` 抛 `OSError` 或崩在两步之间；文件本身可解，只是没登记。真频发了做补登记工具，**不是**退回双真源 |
| 404 / 503 诊断分档塌缩 | "挑错 track"与"首段还在转码"分不开 | 收口后域里已无第二个入口。文案改成同时提示两种可能 |
| `list_segments` 名字留用 | 只做真值判断的老调用点会静默换判据 | 见 §2；仓库内 3 处全是要换的 |

**`SegmentFinder`（`app/services/traceback/segment_finder.py`）不在本次范围**：它有自己的一套
文件系统枚举，服务离线反查。它与 hls 域的清单枚举现在会对"未登记的段"给出不同答案——那正是
收口要的差别，但两者何时合并成一个真源尚未决定。

## 6. 测试

`pytest tests/` 780 通过。新增/改写：

- `TestNoFilesystemEnumerator`：门禁从"名字没了"换成"名字绑在 `_read` 上"（§2）
- `TestSegments`：登记失败的孤儿段 / 手写条目 / 串轨条目都不算段
- `TestSelectSegments`：`list_segments_in_range` 与 `list_segments` 同容器、同源
- `test_playlist_404_keeps_the_structured_body`：钉住 404 的 body 形态
- `tests/factories.py::seed_hls_segments`：铺段 + **登记进清单**的共用播种器（不走
  `insert_segment` 只为免拉 cv2/ffmpeg，落盘形态一致）

**仍然欠着**：`/lab-f3m8/submit` 零行为测试，`/task/history`、`/lab-f3m8/tasks` 没有"盘上有段
但未登记"的路由级用例。
