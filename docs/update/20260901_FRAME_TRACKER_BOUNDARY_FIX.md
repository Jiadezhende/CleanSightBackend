# 离线帧反查修复：段级/帧级边界 off-by-one、位级精确契约、写侧 sidecar 竞态

> **变更状态**：生效中（2026-09-01）——`FrameTracker.find()` 此前在**任何真实查询下必然失败**，本次修复后端到端 1800/1800 帧对齐。
> **知识库**：待沉淀
>
> 承接：建立在 [20260830_FRAME_TRACKER_SIDECAR.md](20260830_FRAME_TRACKER_SIDECAR.md) 的段级 sidecar 方案之上。该方案本身经实测是可靠的（打最小补丁后 1800 帧 ts 位级相等 + 像素逐帧对齐），本次改的是它读侧的边界实现与写侧落盘顺序，不动索引格式。

## 概述

- **改了什么**：修 [frame_tracker.py](../../app/services/inference/offline/frame_tracker.py) 的两个边界 off-by-one（P0）+ 帧只读 / 无 timeout / stderr 死锁风险 / 假容差 / filter 顺序（P1-P2）；修 [hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py) 的 sidecar 落盘顺序，消掉 260ms 竞态窗口。补 seam 单测与端到端 round-trip 测试。
- **为什么改**：验收该工具时实测发现 `FrameTracker.find()` 全部抛 `ValueError: 未找到 ts`。该模块在 `app/` 与 `tests/` 中**零调用方、零测试**，所以 bug 活到了现在。
- **影响面**：离线帧反查读侧行为（从"必然失败"变为可用）、HLS 写侧的段内落盘顺序（对外产物不变，只是 `.idx` 先于 `.mp4` 出现）。在线推理链路不受影响。

实测证据来自 12 段 × 150 帧 @15fps 的真实写路径造数（帧内编码 frame_id，走 `HLSPersistenceStrategy` 落盘再读回）：

| 编号 | 问题 | 实测表现 | 定级 |
|------|------|---------|------|
| #1 | 段级 `searchsorted(side='left')` 取到 start_ts **之后**的段，包含目标的段被跳过 | 跨段 221 帧只出 171；`find()` 全部 `ValueError` | P0 |
| #2 | `iter(None, None)` 拿**段起始**数组的首尾当时间轴首尾 | 全量 1800 帧只出 1651，末段整段丢 149 帧 | P0 |
| #3 | `np.frombuffer(bytes)` 返回只读视图 | 下游 cv2 原地写抛 `ValueError: assignment destination is read-only` | P1 |
| #4 | 写侧 sidecar 晚于 mp4 落盘；读侧缺 sidecar 抛 `FileNotFoundError` 打断整条迭代 | 竞态窗口 260ms；第 7 段缺 idx → 12 段全读不了 | P1 |
| #5 | ffmpeg 无 timeout；`stderr=PIPE` 全程无人读 | 报错不带 stderr 只能靠猜；坏盘上 `read` 无上限阻塞 | P1 |
| #6 | 声称的 ±100µs 容差是死代码 | 有效容差 ≈1e-7s，漂移 1µs 即失败 | P1 |
| #7 | `-vf` 里 `scale` 写在 `select` 前 | 目标 320×240 时慢 ~4%（56.7 → 54.4ms） | P2 |

> **不在本次范围**：性能与稳定性本身没问题——顺序吞吐 2082 fps（≈139× 实时）、RSS 增量 3.2MB 无漂移、提前放弃生成器 ffmpeg 残留 +0 / fd +0、4 线程并发正确、段损坏 0.4s 内失败不挂起。

## 改动详情

### 1. `app/services/inference/offline/frame_tracker.py` — 段级边界（#1、#2）

#### 旧
```python
lo, hi = 0, len(self._segs) - 1
if start_ts is not None:
    lo = np.searchsorted(self._timestamps, start_ts * 1e6, side='left')
    lo = max(0, min(len(self._segs), lo))
...
if start_ts is None:
    start_ts = self._timestamps[0] / 1e6     # ← 段起始，不是时间轴首
if end_ts is None:
    end_ts = self._timestamps[-1] / 1e6      # ← 末段的段首，不是时间轴末
```

#### 新
```python
lo = 0 if start_ts is None else max(
    0, int(np.searchsorted(self._timestamps, start_ts * 1e6, side="right")) - 1)
hi = len(self._segs) - 1 if end_ts is None else int(
    np.searchsorted(self._timestamps, end_ts * 1e6, side="right")) - 1
if lo > hi:          # end_ts 早于首段起点时 hi = -1，在此拦下
    return
for seg in self._segs[lo:hi + 1]:
    yield from self._decode_segment(seg, start_ts, end_ts, width, height)   # None 原样下传
```

> **判据（可复用）：索引里存的是"段起始"，查询问的是"哪个段包含 ts"，两者差一个 `side` 方向。** 要定位**包含** `x` 的区间，在区间起点数组上必须用 `side='right') - 1`；`'left'` 返回的是 `x` **之后**的那个区间起点。
>
> 而且**不存在"大部分情况下对"**：段文件名 `ts_us = int(ts * 1e6)` 是截断值，所以"start_ts 恰为该段首帧"时 `start_ts * 1e6 > ts_us`，`'left'` 同样跳过该段。截断把唯一可能侥幸的情形也堵死了——这也是为什么该 bug 在任何真实查询下都必然触发。
>
> **段起始数组推不出时间轴末端**：`self._timestamps[-1]` 是末段的**段首** ts，其后还有整整一段的帧。默认区间不能靠它兜底，`None` 必须一路下传到帧级裁剪，由后者按"该侧不设限"处理。

### 2. 同文件 — 帧级边界：`searchsorted` 的返回不要 clamp（#1、#2）

#### 旧
```python
k_start, k_end = 0, len(sidecar) - 1
if sidecar[k_start] < start_ts:
    k_start = max(0, min(len(sidecar), np.searchsorted(sidecar, start_ts, side='left')))
if sidecar[k_end] > end_ts:
    k_end = max(0, min(len(sidecar), np.searchsorted(sidecar, end_ts, side='right') - 1))
```

#### 新
```python
k_start = 0 if start_ts is None else int(np.searchsorted(sidecar, start_ts, side="left"))
k_end = len(sidecar) - 1 if end_ts is None else int(
    np.searchsorted(sidecar, end_ts, side="right")) - 1
if k_start > k_end:
    return
```

> **`searchsorted` 的返回天然自洽，clamp 反而制造 bug。** `k_start ∈ [0, len]`、`k_end ∈ [-1, len-1]`，任何空区间都会落到 `k_start > k_end` 被后面一句拦下。旧代码的 `max(0, ...)` 把 `k_end = -1`（end_ts 早于本段首帧）"救"成 0，于是**空区间被误判成命中第 0 帧**。段级同理，只在 `lo` 侧保留 `max(0, ...)`（start_ts 早于首段 → 从首段起），`hi` 侧一律不 clamp。

### 3. 同文件 — 帧可写 + ffmpeg timeout + stderr（#3、#5）

三处一起改在 `_run_ffmpeg`：

- **可写**：每帧新建 `bytearray` 并 `readinto`，`np.frombuffer(bytearray)` 得到的数组**可写且与 buffer 共享内存**；比旧的 `read()`（分配 bytes）+ 只读视图**少一次分配**，不是加开销（实测吞吐从 2022 → 2082 fps）。buffer 每帧必须新建：上一帧已经交给调用方，复用会就地改写别人手里的像素。
- **timeout**：按段给预算（一段是有界工作量 ≤ sidecar 条数），沿用 [step_exporter](../../app/services/lab/step_exporter.py) 的 `max(FLOOR, n × PER_UNIT)` 口径，新增 `_DECODE_TIMEOUT_FLOOR_S = 30` / `_DECODE_TIMEOUT_PER_FRAME_S = 0.2`（实测整段 150 帧 73ms，余量约 400×，只兜"坏盘/网络盘上永久阻塞"）。实现用 daemon `threading.Timer` 看门狗 → `proc.kill()` → stdout EOF → 短读抛错，**不在 `BufferedReader` 上混用 `select`**（缓冲区里已有的数据 `select` 看不见）。
- **stderr**：`stderr=tempfile.TemporaryFile()` 而非 `PIPE`。

> **`stderr=subprocess.PIPE` 且全程无人读 = 潜在死锁**：管道缓冲区（Linux 默认 64KB）写满后 ffmpeg 阻塞在 write，读侧阻塞在 read，双向卡死。旧代码只是靠 `-loglevel error` 让它写不满而已，一旦 ffmpeg 转成话痨（新版本、新告警、损坏输入刷屏）就会撞上。改落临时文件后既没有这个上限，失败时还能 `seek(0)` 把 ffmpeg 的原话带进异常——实测损坏段的报错现在直接带出 `Invalid data found when processing input`，不必靠猜。

### 4. 同文件 — `_load_sidecar` 缺失降级 + `_build_cmd` filter 顺序（#4、#7）

`_load_sidecar` 从 `raise FileNotFoundError` 改为 `logger.warning` + 返回空数组 → 该段被跳过，不打断整条迭代。**缺一段的索引不该让前后所有段一起读不了**；单点查询仍会在 `find()` 里因目标 ts 缺失而硬失败，不会静默配错帧。

`_build_cmd` 的 `-vf` 从 `scale,select` 改成 `select,scale` —— 反过来会把注定被丢弃的帧也缩放一遍。

### 5. 同文件 — `FrameTracker.find` 契约（#6）

#### 旧
```python
if abs(frame.timestamp - sorted_timestamps[idx]) <= 1e-4:
```

#### 新
```python
if not timestamps:
    return
sorted_timestamps = sorted(float(t) for t in timestamps)
idx = 0
for frame in self._tl.iter(sorted_timestamps[0], sorted_timestamps[-1], width, height):
    while idx < len(sorted_timestamps) and frame.timestamp == sorted_timestamps[idx]:
        yield frame                    # while 而非 if：重复 ts 按重数各产出一帧
        idx += 1
if idx < len(sorted_timestamps):
    raise ValueError(f"未找到 ts={sorted_timestamps[idx]!r} 对应帧")
```

> **容差契约翻案：那个 `1e-4` 从来没生效过。** 帧级裁剪用的是精确比较——目标 ts 只要偏离真实帧 ts，`searchsorted` 就先把目标帧切出区间了，压根轮不到 `abs(...) <= 1e-4` 判断。实测有效容差 ≈1e-7s：漂移 ±1e-9 / +1e-7 命中，±1e-6 起即失败。
>
> **现明确要求位级精确**，理由是这本来就成立：`timestamps` 应取自同一 run 的 `features.jsonl` / `FeatureStore.load()`，`json.dumps` 的 float repr + `float()` 回读是位级 round-trip，[store.py](../../app/services/inference/feature/store.py) 已把"feature 行可按 ts 精确对上同帧的 HLS 证据片段"写成契约。删掉假承诺比留一个从不生效的 `1e-4` 更诚实——**ts 是帧的身份，配错帧比报错更坏**。任何精度中转（float32、重新格式化）都会 `ValueError`，这是设计而非缺陷。
>
> 顺带补了 docstring 写死另外两条：**产出顺序是 ts 升序，不保证与入参同序**（调用方按 `frame.timestamp` 对号入座，勿按位置）；空入参返回空。

### 6. `app/services/persistence/strategies/hls_strategy.py` — 消掉 260ms 竞态（#4）

`_update_timeline` 的调用点从 `_persist_raw_segment` **末尾**移到 `cv2.VideoWriter` **之前**（新的步骤 0），使 sidecar 先于 mp4 存在。同时把它函数内的 `import numpy as np` 提到模块顶部。

> **写侧 sidecar 必须先于 mp4 落盘。** `SegmentFinder` 认的是 `raw_segment_*.mp4`——mp4 一出现，该段就对离线反查可见。旧顺序（先 mp4、后 idx）留下一个"段可见但索引未就位"的窗口，**实测 260ms**，覆盖 mp4 编码 + transcode 全程。反过来的代价是 mp4 写失败时残留孤儿 `.idx`，但它对 `SegmentFinder` 不可见，随 `purge_step_dir` 的 rmtree 一并回收——远小于那个窗口。
>
> 两侧都修：写侧关窗口，读侧（见改动 4）对残余窗口降级跳过。

### 7. 保留项（不改动）

- sidecar 的格式（float64 ts 原值数组，无 tick / 无 first_ts）、`tofile(tmp)` + `os.replace` 原子写、取消索引持锁——[20260830](20260830_FRAME_TRACKER_SIDECAR.md) 的这些决定都成立，本次未动。
- `concat:raw_init.mp4|seg.mp4` + `select=between(n,k1,k2)` + `-vsync 0`、无 `-ss`、无 m3u8、无像素缓存——端到端验证证明这条解码路径**没有帧漂移**（1800 帧逐帧像素 id 匹配），是可信的。
- `finally: proc.kill(); proc.wait()`——实测生成器提前放弃时 ffmpeg 残留 +0 / fd +0，有效，保留。

## 测试

| 文件 | 性质 | 说明 |
|------|------|------|
| [tests/test_frame_tracker_boundary.py](../../tests/test_frame_tracker_boundary.py) | seam 单测，进 `pytest tests/` | 边界数学是纯逻辑：子类覆盖 `_run_ffmpeg` 产出合成 Frame，**不起 ffmpeg**。造数只需空 `raw_segment_{ts_us}.mp4` 占位 + 真 `.idx`（`SegmentFinder` 只解析文件名）。复用 `tmp_storage` fixture |
| [integration_tests/test_frame_tracker_roundtrip.py](../../integration_tests/test_frame_tracker_roundtrip.py) | 端到端，手动运行 | 只依赖 ffmpeg，**不需要 RTSP / DB / 后端服务 / 真实告警**。走真实 `HLSPersistenceStrategy` 落盘再读回，帧内中心色块编码 frame_id（三通道 16 阶量化，抗 H.264 有损）逐帧比对。写 `database/9900002/` 后自清理 |

> **两者互补，都要跑。** seam 单测覆盖全部边界情形但抓不到"解码出来的像素是不是那一帧"；round-trip 是**唯一能抓 ts ↔ 像素错配的手段**，但慢（约 10s）且需要 ffmpeg。

## 调用方指引

- **批量回看按 ts 排序后走 `Timeline.iter(start, end)` 一次扫过，别逐条 `find()`**：单点反查代价 36–73ms（无 `-ss`、无关键帧 seek，段内帧号越大越慢，因为要顺序解到那一帧），而顺序全量是 2082 fps。逐条查 N 个点 ≈ N 次整段解码。
- `find()` 产出 **ts 升序**，不保证与入参同序 —— 按 `frame.timestamp` 对号入座。
- 传入的 ts 必须**位级等于** sidecar 中的帧 ts（同一 run 的 `features.jsonl` / `FeatureStore.load()`），任何精度中转都会 `ValueError`。

## 验证

| 项 | 结果 |
|----|------|
| `pytest tests/test_frame_tracker_boundary.py` | 24 passed；对修复前代码跑同一份用例 **15 failed**（确认用例有牙） |
| 全量 `pytest tests/` | 444 passed |
| `python integration_tests/test_frame_tracker_roundtrip.py` | **13 / 13 PASS**（修复前对应项分别是 1651/1800、跨段 171/221、`find()` 全 `ValueError`、缺 sidecar 整条中断、返回帧只读） |
| 端到端关键判据 | 全量 1800/1800 帧 ts 位级相等 + 像素 frame_id 逐帧匹配；缺 sidecar 时只丢该段 150 帧、其余 1650 帧照常；漂移 1µs 的 ts 抛 `ValueError`；返回帧 `flags.writeable is True` |
| 看门狗 / stderr | 预算压到 1ms 时 0.01s 内抛 `ffmpeg timeout after 0.001s from ...`；段被填零后 0.37s 抛 `Incomplete frame ...; ffmpeg stderr: Invalid data found when processing input` |

**性能基线（供后续回归对比）**

| 指标 | 修复前 | 修复后 |
|------|-------|-------|
| 顺序全量吞吐 | 2022 fps | **2082 fps**（`readinto` 少一次分配） |
| 单点反查延迟（随段内帧号线性增长） | 42 → 73ms | 36 → 73ms |
| 3 轮全量遍历 RSS 增量 | 2MB 无漂移 | 3.2MB 无漂移 |
| 提前放弃生成器 ×20 的 ffmpeg / fd 残留 | +0 / +0 | +0 / +0 |
| 4 线程并发加速比 | 2.4× | 2.4×（5004 fps） |
| 索引体积 | 8B/帧 ≈ 视频体积 0.29%（1800 帧 = 14.4KB vs 5.0MB） | 同 |
