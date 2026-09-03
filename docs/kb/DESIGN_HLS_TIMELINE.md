> 更新时间：2026-09-02
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

## 媒体时间基：`mdhd.timescale` pin 死 90000

**tick 尺度必须是全轨常量，不能让 ffmpeg 按段自选。** `{track}_init.mp4` 只由该轨首段生成、
被整条 playlist 复用（`#EXT-X-MAP` 声明的就是它的时间基）；而不指定时 ffmpeg 按「fps 有理数
约分分子 × 2^k」自选 timescale——逐段 `eff_fps` 不同即逐段 timescale 不同，后续 fragment 的
tick 被按首段尺度解读，误差是**乘性**的。

- 实测：15fps 定 init、14.37fps 的段（自选 timescale=11496）→ 声明 10.02s 被读成 7.60s，
  单段 2.4s 空洞，hls.js 段尾停摆。
- 自选值对 fps **极不连续**（15.0→15360，但 14.37→11496，分子 1437 约不动），fps 抖 4% 可致
  timescale 差 25%——「fps 波动不大就没事」不成立。
- 取 **90000**（MPEG-TS/RTP 标准视频时钟）：整除 30/25/24/20/15/12/10 等常见帧率；非整除帧率下
  ffmpeg 按绝对 PTS 取整增量差分，误差有界 ≤ 半 tick（5.6µs）且不累积。
- 传法有坑：必须 `-hls_segment_options video_track_timescale=90000` 透传给**内层 mp4 muxer**，
  直接给 hls muxer 传 `-video_track_timescale` 会被**静默忽略**。

pin 之后 timescale 与编码 fps 彻底解耦，逐段 `eff_fps` 才是合法的速率表达；tfdt 累计偏移也因此能
按常量换算 tick，无需回读产物。

> **tfdt 靠 hex-patch 而非 ffmpeg 参数**：ffmpeg 8.x 的 HLS muxer + fmp4 在 `-start_number 0`
> 下强制清零 tfdt、丢弃 `-output_ts_offset`，只能转码完直接改 `moof/traf/tfdt.baseMediaDecodeTime`
> 这个 box，写入「已入 playlist 的累计 EXTINF × 90000」。三套时间线（EXTINF / tfdt / fragment
> 媒体时长）必须对齐到同一真值。

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

## 逐帧 ts sidecar（`.idx`）与离线帧反查

raw 轨每段配一个同名 sidecar `raw_segment_{ts_us}.idx`：该段每帧 `frame.timestamp` 的 float64
**原值**数组，一帧一条——无 tick、无 first_ts、不依赖 `metadata.json`，`tofile(tmp)` + `os.replace`
原子写、不持锁。processed 轨不产（渲染结果离线不消费）。

**查询链路**：`ts → SegmentFinder 按文件名 ts_us 定位段（内存 searchsorted，零文件读）→ 段内读
sidecar searchsorted 得帧号区间 → concat:{raw_init.mp4}|{seg.mp4} + select=between(n,k1,k2)
+ -vsync 0 解码 → 流式 yield Frame`。不拼 m3u8、不用 `-ss`，因此**段内帧号与 sidecar 下标严格 1:1**。

### 写读顺序：`.idx` 必须先于 `.mp4` 落盘

读侧判定「段可读」的唯一依据是 **mp4 文件存在**——`SegmentFinder` 只 `iterdir` + 匹配文件名，不看
大小、不看写完没有，而 `cv2.VideoWriter` 一构造文件就在磁盘上了。sidecar 若排在 mp4 之后，就留下
一个「段可见但索引未就位」的窗口，实测 **260ms**（覆盖 mp4 编码 + transcode 全程）；期间跑一次离线
迭代就会撞上无索引的段。故 `_update_timeline` 排在 `VideoWriter` 之前。代价是 mp4 写失败时残留孤儿
`.idx`，但它不匹配段名模式、对 `SegmentFinder` 不可见，随 `purge_step_dir` 的 rmtree 回收。

**两侧都要容错，窗口关不干净**：sidecar 写失败只 warning（不能拿主产物给辅助索引陪葬），历史段本就
没有 `.idx`——「有 mp4 无 idx」是合法状态。读侧缺 sidecar 时**跳过该段、不打断整条迭代**：缺一段的
索引不该让前后所有段一起读不了（该异常一抛会穿透 `yield from` 打死整个生成器）。

### ts 是帧的身份：位级精确，无容差

`FrameTracker.find(timestamps)` 要求传入 ts **位级等于** sidecar 中的帧 ts，任何精度中转（float32、
重新格式化）都抛 `ValueError`——这是设计而非缺陷：ts 应取自同一 run 的 `features.jsonl` /
`FeatureStore.load()`，`json.dumps` 的 float repr + `float()` 回读是位级 round-trip，**配错帧比
报错更坏**。产出顺序恒为 ts 升序，不保证与入参同序，调用方按 `frame.timestamp` 对号入座。

### 段级定位：区间起点数组要 `side="right") - 1`

索引里存的是**段起始** ts，查询问的是**哪个段包含** ts，两者差一个 `side` 方向——`"left"` 返回的是
ts **之后**的那个段。且段名 `ts_us = int(ts*1e6)` 是截断值，连「ts 恰为该段首帧」这种可能侥幸的情形
也被堵死，故用错方向是无条件错。同理，**段起始数组推不出时间轴末端**（`_timestamps[-1]` 是末段的
段首，其后还有整整一段的帧），默认区间的 `None` 必须一路下传到帧级裁剪，由后者按「该侧不设限」处理。

### 调用代价

顺序全量吞吐 ≈2000 fps（百倍于实时），单点反查 36–73ms 且随段内帧号线性增长（无 `-ss`、无关键帧
seek，要顺序解到那一帧）。**批量回看按 ts 排序后走 `Timeline.iter(start, end)` 一次扫过，别逐条
`find()`**——逐条查 N 个点 ≈ N 次整段解码。索引体积 8B/帧 ≈ 视频体积的 0.3%。

## 代码来源

- `app/services/persistence/strategies/hls_strategy.py`（写侧唯一真源：段/init/playlist/sidecar）
- `app/services/inference/offline/frame_tracker.py`（读侧：`Timeline` / `FrameTracker`）
- `app/routers/traceback.py`
- `app/services/traceback/segment_finder.py`
- `tests/test_traceback_router.py`
- `tests/test_lab_clip_builder.py`
- `tests/test_frame_tracker_boundary.py`、`integration_tests/test_frame_tracker_roundtrip.py`

