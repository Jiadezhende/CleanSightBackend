> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# HLS 时间线设计

**墙钟轴与媒体轴是两条轴，段间空隙只存在于墙钟轴。** 同一批帧派生出三个时间量（文件名 `ts_ms`、
`EXTINF`、`tfdt`），混用任一对都不报错，只是画面或指针错位。两轴换算只有一个载体 `MediaTimeline`；
「有哪些段」只由清单回答。

落盘布局与 TTL 回收见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)，
切段与落盘时机见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)，写入事务（stage → adjust → commit）
见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)；本文只管时间轴。

---

## 1. 三个时间量各答一个问题

```text
帧流   t_0 … t_{N-1}   │   t'_0 … t'_{M-1}   │  [断流 30s]  │   t''_0 …
       └── 段 0，N 帧 ─┘   └── 段 1，M 帧 ───┘              └── 段 2

                  段0             段1        ·····空隙·····        段2
① 墙钟轴  ├──── 10s ────┼──── 10s ────┤···················├──── 10s ────┤
         100.0s        110.0s       120.0s              140.0s       150.0s
  文件名  ..100000      ..110000                         ..140000

② 媒体轴  ├──── 10s ────┼──── 10s ────┼──── 10s ────┤
         0s            10s           20s           30s
  EXTINF  10.000        10.000        10.000
  tfdt    0             900000        1800000                 (tick, ×90000)
```

| 时间量 | 落在哪 | 回答什么 |
|--------|--------|---------|
| 文件名 `ts_ms` | `{track}_segment_{ts_ms}.mp4` | 这段是什么时候拍的——检索、定位、告警对号 |
| `EXTINF` | `{track}_playlist.m3u8` | 这段有多长——播放器估算 |
| `tfdt` | fragment 内 `moof/traf/tfdt` | 这段字节放在媒体轴哪里——播放器落点 |

播放器只看媒体轴。`raw` / `processed` 两轨各自切段，**各有各的媒体轴**，一轨的媒体刻度拿到另一轨上无意义。

### 文件名 ts 差不是段时长

记 `Δ̄` 为段内平均帧间隔：

```text
span     = t_{N-1} - t_0   = (N-1)·Δ̄
EXTINF   = N / eff_fps     = N·Δ̄ = span + Δ̄     多出的 Δ̄ 是末帧自身的显示时长
ts 跨度  = t'_0 - t_0      = (N-1)·Δ̄ + gap_边界
EXTINF − ts 跨度 = Δ̄ − gap_边界
```

切段按帧数切，`gap_边界` 与段内帧间隔同分布，所以误差零均值、量级一个帧间隔（15fps 下 ±67ms），
累计是随机游走而非单调漂移。**例外是重连后的首段**：此时 `gap_边界` 是真实停顿，差值就是空洞本身
（§4 的断流判据正用它）。

### 墙钟终点取 `max(段起点 + EXTINF)`

取 `max(段起点)` 会漏掉末段自身时长。算法只有一处：`hls.query_span(run, tracks)` →
`HlsSpan.end_ms = max(ts_ms + round(EXTINF×1000))`，traceback / task / lab 共用。`/timeline` 传双轨，
得到的是**双轨并集**的墙钟跨度，不能与单轨 Σ EXTINF 相减（§4）。

---

## 2. 段时长由 `eff_fps` 反推，不接受上游 fps

`_encode.effective_fps(frames) = (N-1) / (ts_last - ts_first)`，不引用任何上游 fps。退化段
（`span<=0`、单帧、反推值落在 `[1.0, 60.0]` 外）回落 `_DEGENERATE_FALLBACK_FPS = 15.0`。

**同一个 fps 同时喂给 `cv2.VideoWriter` 和 `EXTINF`**（`EXTINF = N / eff_fps`），写入帧率与声明时长
才一致。用固定 fps 写入实际帧数更少的段会快放（实测 2x）；用墙钟帧 ts 差当 EXTINF 会造成 hls.js
段尾 MSE 缓冲洞、卡死和总时长缩水。退化段仍自洽（tfdt / mdhd / EXTINF 一致，播放不出洞），只是
墙钟↔媒体换算失真。

---

## 3. 媒体时间基与 ffmpeg 写法约束

### `mdhd.timescale` 必须 pin 死 90000

`{track}_init.mp4` 只由该轨首段生成、被整条 playlist 复用。不指定时 ffmpeg 按段自选 timescale
（fps 有理数约分后的分子 × 2^k），逐段 `eff_fps` 不同即逐段 timescale 不同，后续 fragment 的 tick
按首段尺度解读，误差是**乘性**的：

- 实测：15fps 定 init、14.37fps 的段（自选 11496）→ 声明 10.02s 被读成 7.60s，单段 2.4s 空洞，hls.js 段尾停摆。
- 自选值对 fps 极不连续（15.0→15360，14.37→11496），fps 抖 4% 可致 timescale 差 25%。
- 90000 整除常见帧率（30/25/24/20/15/12/10）；非整除帧率下误差 ≤ 半 tick（5.6µs）且不累积。
- **写法**：`-hls_segment_options video_track_timescale=90000` 透传给内层 mp4 muxer；直接给 hls
  muxer 传 `-video_track_timescale` 会被**静默忽略**。

pin 之后 timescale 与编码 fps 解耦，tfdt 累计偏移可按常量换算 tick，无需回读产物。

### tfdt 只能 hex-patch

每段由独立 ffmpeg 进程转码、输入从 PTS=0 起，不补偏移则所有 fragment 落点都是 0，播到第一段末尾
就不前进。ffmpeg 8.x 的 HLS muxer + fmp4 在 `-start_number 0` 下强制清零 tfdt，`-output_ts_offset`、
`-itsoffset`+`-copyts`、`-muxdelay` 均无效（8.x 实测；现钉版 n7.1.4 未单独复测，待核验）。
所以转码完直接改 `moof/traf/tfdt.baseMediaDecodeTime`，写入「已入清单的累计 EXTINF × 90000」；
box 结构与 size 不变。找不到 box 或 v0 装不下时**整段作废**，不放行落点错误的 fragment。

### 转码子进程 `cwd=输出目录`，输出参数只给 basename

两个 ffmpeg 大版本对 `-hls_fmp4_init_filename` 的路径解析正好相反：8.x 把 basename 解析到进程 cwd；
4.x 把绝对路径当相对路径拼到 playlist 目录前（得到 `/dir/foo/dir/foo/init.mp4` → ENOENT）。唯一兼容
写法是子进程 `cwd` 落在输出目录（stage 目录）、所有输出只给文件名。改成绝对路径只在换版本或换发行版时
才炸。现在 Linux / Windows 都用 `.ffmpeg/bin` 钉版 n7.1.4，这条仍保留，防换版踩坑。

---

## 4. 墙钟↔媒体换算只经 `MediaTimeline`

媒体轴是压紧的墙钟：「首段墙钟 + 媒体刻度」只在从没断过流时成立，断过就偏早整整一个空洞。换算要读
清单，所以调用侧（`/timeline` 告警落点、`clip_builder` 裁剪区间、`/ai/temporal` 与
`/lab-f3m8/label-probs` 的帧 ts 换算）一律经 `MediaTimeline`，不自己凑。

**媒体轴归数据层，断流阈值归业务层**：媒体轴是清单的纯推导，存储域只出不带阈值的逐对空隙；
「多大空隙算断流」在上层。

```text
app/storage/hls/_timeline.py         MediaTimeline / PlacedSegment（不碰盘）：select / media_offset_ms /
                                     wall_ms_at / media_ms_at / duration_ms / wall_gaps()（不带阈值）
app/storage/hls/_read.py             query_timeline(run, track)：读清单展开，构造媒体轴的唯一入口
app/services/utils/media_timeline.py GAP_THRESHOLD_MS、first_gap(tl)、total_gap_ms(tl)
```

### 落点：清单顺序累加 EXTINF，出口才取整

`query_timeline` 把清单展开成 `PlacedSegment(seg, media_start_ms)`：清单顺序即时序，累加 EXTINF 即落点。
累加用 float 秒、只在出口取整到 ms；逐段取整再累加会线性累积误差（180 段最坏 90ms）。

- 刻度一律相对整条轨原点，`select(start, end)` 取子集后**不重新归零**，调用方的绝对刻度才能直接相减。
- `media_offset_ms(media_ms)` 是交给 ffmpeg `-ss` 的数：取子集时 ffmpeg 把首段 tfdt 归一到 0（实测绝对
  落点 100s 的子集照样 `start_time=0`），所以偏移是纯减法。
- **前提：EXTINF 落盘精度是整毫秒**（`_m3u8.entry()` 用 `:.3f`）。若提到六位小数，`query_timeline` 须改
  整数累加，否则段间 ±1ms 错位，`select()` 边界漏段、`media_offset_ms` 变负。

### 落进空洞的墙钟吸附到下一段段首

`media_ms_at(wall_ms)` 遇到空洞时返回下一段段首：空洞在媒体轴上宽度为零，吸到下一段段首才不会声称
某一帧拍于空洞之中。超出整条轨时贴最近一端；`wall_ms_at` 同样贴边，所以区间尾越过轨尾时产物会短，
调用方要把自己记的时长跟着收（`clip_builder` 即如此）。

### 空洞只能在墙钟轴上判

```text
gap = 下一段起点 − (本段起点 + EXTINF) > GAP_THRESHOLD_MS
```

两项都是真值（文件名实测墙钟、清单段时长）。fps 漂移会同时压低 `eff_fps` 和段内帧数，`EXTINF` 跟着
变长，所以漂移被天然吸收。媒体轴上相邻段永远首尾相接，判不出空洞；段尾也不能用「下一段起点」代替，
那会把停顿算进段长。

### `GAP_THRESHOLD_MS = 500` 是保守护栏

- 下界：正常段边界残差（§1 的 `Δ̄ − gap_边界`），典型 ≈0、尾部几十 ms。
- 上界：能触发重连的断流（decoder 退出 → 健康检查 1s 内发现 → respawn → RTSP 重连），秒量级。

0.5s 落在两者之间，且不随段长或 fps 变。要精确判据，得等 health_monitor 声明断点真值。

### 累计断流只用 `total_gap_ms(tl)`

它是超阈空隙之和。**不要用「墙钟跨度 − Σ EXTINF」凑**：`/timeline` 的 `duration_ms` 是双轨并集跨度，
Σ EXTINF 是单轨的，差值里混着两轨起止不对齐（推理起步晚于取流时，processed 首段晚于 raw 首段），
零断流的 run 也会算出假空洞。

### 媒体落点的三个表示必须相等

```text
写侧   tfdt            hex-patch 进 fragment 的 baseMediaDecodeTime ÷ 90000
浏览器 fragment.start  hls.js 按清单 EXTINF 累加
后端   media_start_ms  hls.query_timeline 按同样方式累加
```

三者岔开都不报错：进度条指针与画面错位、跳转跳错、裁剪裁到隔壁段。

---

## 5. 段集合只由清单回答

`hls.list_segments` 纯解析清单，一行条目同时给出墙钟锚点（URI 里的 `ts_ms`）和媒体长度（EXTINF），
不枚举目录；`list_segments_in_range` 同源，只多一次段级区间切片。盘上有文件而清单无条目的不算段：
可能是在途段，也可能是登记失败的段（段已就位、清单追加抛 `OSError`）。喂给播放器会出现 MSE 缓冲洞；
喂给 ffmpeg 会 exit 0、无日志、产出少一截的 mp4（[DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md) §5.3）。

- 写侧在 `.stage_{track}_{ts_ms}/` 里编码转码，commit 后才出现在正式位置，所以没有在途段窗口
  （原地编码会留下「文件在、但还是 mp4v」的窗口，实测 ~260ms）。
- 清单只追加，顺序即时序。`list_segments` 仍按 `ts_ms` 排一次（下游 `bisect_right - 1` 依赖升序），
  **真出现逆序时记 warning**：tfdt 是按清单顺序累加的，静默重排会让读侧媒体偏移与文件里的 tfdt 对不上，
  seek 到错帧也不报错。

---

## 6. init 按轨分存

`raw_init.mp4` / `processed_init.mp4` 分存：两轨是两条独立 playlist，各有各的 `#EXT-X-MAP`，共用文件名
会变成「谁先转码谁定」。init 只在该轨首次写入时落盘，已存在则丢弃新的（同轨编码参数一致，换新的只会
让已发布清单换 init）。

缺 init 时回放与整段导出返回 **503**（此 run 暂不可用，不是资源不存在）。判定顺序上，「一个段都没有」
的 404 必须先于缺 init 的 503，否则不存在的 task/step 会拿到「请重试」语义。commit 顺序是 init 先于
清单条目（§8），所以清单非空时 init 必已落盘；503 只在 init 被外部删除时出现，是防御分支。

---

## 7. 同一 `(run, track)` 的写必须串行

相邻段的 tfdt 要读清单求累计 EXTINF。两段并发进来会读到同一个累计值，tfdt 碰撞，后段在播放器里覆盖
前段：**不报错、不卡顿，只是画面丢一截**。不同 run、不同轨各写各的段、init 和清单，互不冲突。

数据层不持锁，由调用侧提交到单消费者 `SerialTaskQueue`（recording 的段队列，因此不能加 worker）。
没有需要与写同序的删除：一 run 一目录，换代写新目录；TTL 回收把 step 整体原子 rename 进回收区，
写者不建 run 目录，回收后的迟到写原子失败。

---

## 8. 逐帧 ts sidecar（`.idx`）与离线帧反查

媒体轴是压紧的，段内第 k 帧的墙钟 ts 从 mp4 里问不出来，所以 raw 轨每段配一个 `raw_segment_{ts_ms}.idx`：
该段每帧 `frame.timestamp` 的 float64 原值数组，无头，条数 = 文件大小 ÷ 8，经 `app.storage.utils.fs.replace`
原子写。**processed 轨不产**（离线不消费），所以 `hls.iter_frames` 不设 `track` 参数，`read_segment` 收到
非 raw 的 ref 直接 `ValueError`。

目前 `iter_frames` / `read_segment` 只有测试与集成测试调用，为离线 ROI 视觉特征预留。

**查询链路**：

```text
ts 区间 → list_segments_in_range 段级裁剪 → 段内 sidecar searchsorted 得帧号区间
       → ffmpeg -i concat:{raw_init.mp4}|{seg.mp4} -vf select=between(n,k1,k2),scale=… -vsync 0
       → 流式 yield Frame（像素来自 mp4、ts 来自 .idx）
```

- **不拼 m3u8、不用 `-ss`**：`-ss` 按时间 seek，会让帧号原点漂掉；帧上的 ts 看着正确，像素却是别的帧。
  只有这样，段内帧号与 sidecar 下标才严格 1:1。
- `select` 必须排在 `scale` 之前，否则注定丢弃的帧也被缩放一遍。

### commit 顺序：sidecar → init → 段文件 → 清单条目

```text
sidecar 先于段文件     反过来留下「段可见但索引未就位」的窗口，离线反查拿不到 ts
init 先于清单头        清单头的 EXT-X-MAP 指向不存在的文件 = 播放器报错
段文件先于清单条目     清单有行无文件 = 播放器报错
```

三条写反都不报错。读侧判定「段存在」只看清单条目；mp4 写失败留下的孤儿 `.idx` 不在清单里，随 TTL 回收。

**两侧都容错**：sidecar 写失败只 warning（不拿主产物给辅助索引陪葬），「有 mp4 无 idx」是合法状态；
读侧缺 sidecar 时跳过该段，不打断整条迭代（异常一抛会穿透 `yield from` 打死整个生成器）。

### ts 是帧的身份：位级相等，不设容差

解码出的 `Frame.timestamp` 位级等于写入值，可直接与同一 run 的 `detections.jsonl`
（`inference.read_detections(run)`）做 `==`：`json.dumps` 的 float repr + `float()` 回读是位级 round-trip。
任何精度中转（float32、重新格式化）都会让配对失败。**配错帧比报错更坏**，所以配对失败宁可显式报错，
不做最近邻匹配。同一原则还落在 `LabelProbs.ts`（与 detections.jsonl 位级相等）上。

域内没有按离散 ts 点查的接口，位级配对由调用方按 `frame.timestamp` 做。

### 段级定位用 `bisect_right(starts, ts) - 1`

清单存的是**段起始** ts，查询问的是**哪段包含** ts。`bisect_left` 返回的是 ts 之后的段。段名
`ts_ms = floor(Fraction(ts) × 1000)` 按 float 精确值向下取整：不能四舍五入（进位会落到前一段），也不能
写 `int(ts*1000)`（`0.29*1000 == 290.0`，而 0.29 精确值略小，段名比首帧晚）。所以即使 ts 恰为段首帧，
用错方向也一定错。

- **段起始数组推不出时间轴末端**（末段段首之后还有一整段帧）：区间缺省的 `None` 必须一路下传到帧级
  裁剪，按「该侧不设限」处理。
- 帧级 `searchsorted` 的返回天然自洽（空区间落到 `k_start > k_end`），刻意不 clamp；把 `k_end = -1`
  救成 0 会把空区间误判成命中第 0 帧。段级同理，`hi = -1` 不 clamp。

### 调用代价

顺序全量吞吐 ≈2000 fps（百倍于实时）；单点反查 36–73ms，随段内帧号线性增长（无 seek，要顺序解到那一帧）。
**批量回看按 ts 排序后用 `iter_frames(start_ts=…, end_ts=…)` 一次扫过**，逐点查 N 个点 ≈ N 次整段解码。
索引体积 8B/帧 ≈ 视频的 0.3%。

---

## 代码来源

- `app/storage/hls/`：`_encode`（eff_fps / mp4v）、`_fmp4`（转码 + tfdt hex-patch + `TIMESCALE`）、
  `_m3u8`（EXTINF 格式与累计）、`_idx`（sidecar）、`_layout`（命名、`ts_to_ms`）、`_read`（清单枚举、
  `query_timeline` / `query_span`）、`_timeline`（`MediaTimeline`）、`_write`（stage/commit）、
  `_decode`（解码与两级裁剪）、`types`（`Segment` / `SegmentRef` / `HlsSpan`）
- `app/services/utils/media_timeline.py`（断流判据）
- `app/services/recording/service.py`（写的 `SerialTaskQueue`）
- `app/routers/traceback.py`、`app/routers/media.py`
- `app/services/lab/clip_builder.py`、`app/services/lab/step_exporter.py`
- `app/types/temporal.py`（`LabelProbs.ts`）
- `tests/test_media_timeline.py`、`tests/test_storage_hls.py`、`tests/test_lab_clip_builder.py`、
  `tests/test_traceback_router.py`、`integration_tests/test_hls_frame_roundtrip.py`（ts↔像素 round-trip）
