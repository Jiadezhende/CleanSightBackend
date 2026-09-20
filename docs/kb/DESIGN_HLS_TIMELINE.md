> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# HLS 时间线设计

**墙钟轴与媒体轴是两条轴，段间空隙只存在于前者。** 同一批帧派生出三个时间量（文件名 `ts_us`、
`EXTINF`、`tfdt`），各自回答不同的问题，混用任一对都不报错、只是画面或指针错位。
**两轴之间的换算只有一个载体 `MediaTimeline`，别在调用侧自己凑。**
**「有哪些段」只由清单回答**，文件系统枚举已整个从域里删除。

落盘目录布局见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)，
谁在什么时机切段、落盘、清理见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)；本文只管时间轴本身。

---

## 1. 三条时间线的全景

```text
帧流   t_0 … t_{N-1}   │   t'_0 … t'_{M-1}   │  [断流 30s]  │   t''_0 …
       └── 段 0，N 帧 ─┘   └── 段 1，M 帧 ───┘              └── 段 2

                  段0             段1        ·····空隙·····        段2
① 墙钟轴  ├──── 10s ────┼──── 10s ────┤···················├──── 10s ────┤
         100.0s        110.0s       120.0s              140.0s       150.0s
  文件名  ..100000000   ..110000000                      ..140000000

② 媒体轴  ├──── 10s ────┼──── 10s ────┼──── 10s ────┤
         0s            10s           20s           30s
  EXTINF  10.000        10.000        10.000
  tfdt    0             900000        1800000                 (tick, ×90000)
```

**空隙只在①，②里不存在。** 段 2 的文件名从 110 跳到 140，但它的 EXTINF 照样 10.000、tfdt 照样
接在段 1 之后。播放器只看②。

| 时间量 | 落在哪 | 回答什么问题 |
|--------|--------|-------------|
| 文件名 `ts_us` | 段文件名 `{track}_segment_{ts_us}.mp4` | 「这段画面是什么时候拍的」——检索、定位、告警对号 |
| `EXTINF` | `{track}_playlist.m3u8` | 「这段有多长」——播放器估算用 |
| `tfdt` | fragment 内 `moof/traf/tfdt` box | 「这段字节放在媒体轴哪里」——播放器落点用 |

两条轨（`raw` / `processed`）**各有各的媒体轴**：各自独立切段，Σ EXTINF 不同尺，一条轨上的媒体
刻度拿到另一条轨上无意义。

### 文件名 `ts_us` 不能当段时长

记 `Δ̄` 为段内平均帧间隔：

```text
span     = t_{N-1} - t_0   = (N-1)·Δ̄            段内首末帧跨度
EXTINF   = N / eff_fps     = N·Δ̄ = span + Δ̄     多出的 Δ̄ 是末帧自身的显示时长
ts 跨度  = t'_0 - t_0      = (N-1)·Δ̄ + gap_边界
EXTINF - ts 跨度 = Δ̄ - gap_边界
```

切段是纯计数切，所以 `gap_边界`（跨段边界那一次帧间隔）与段内任意帧间隔同分布 ⇒ 该误差**零均值、
量级一个帧间隔**（15fps 下 ±67ms），累计是随机游走而非单调漂移。**唯一例外是重连后的首段**——
那时 `gap_边界` 是真实停顿，差值就是空洞本身（§4 的判据正是拿它当信号）。

### step 的墙钟跨度要用 `max(ts + EXTINF)`

`/traceback/task/{id}/timeline` 算 step 起止时，end 取 `max(seg.ts_us + EXTINF)` 而不是
`max(seg.ts_us)`，否则漏掉最后一段自身的时长。该口径覆盖 `raw` / `processed` 两轨取并集，
所以它是**双轨并集的墙钟跨度**——不能拿它跟单轨 Σ EXTINF 相减（§4 末）。

---

## 2. 段时长由 `eff_fps` 反推，不接受任何上游 fps

写侧把帧序列编成 mp4v（cv2），再由 ffmpeg 转成 fMP4 fragment。段的编码帧率
`_encode.effective_fps(frames)` = `(N-1) / (ts_last - ts_first)`，**不引用任何上游 fps**；
退化段（`span<=0` / 单帧 / 反推值落 `[1.0, 60.0]` 带外）回落本地常量
`_DEGENERATE_FALLBACK_FPS = 15.0`。

**同一个 fps 同时喂给 `cv2.VideoWriter` 与 `EXTINF`**（`EXTINF = N / eff_fps`），保证 raw /
processed 段（经推理与可视化后帧率可能非名义值）的写入帧率与声明时长一致，不会快放——曾因固定
fps 写入实测更少帧数的段导致 2x 快放。退化段仍**自洽**（tfdt / mdhd / EXTINF 三者一致、播放不出
洞），只是它的墙钟↔媒体换算失真。

若用墙钟帧 ts 差当 EXTINF，会造成 hls.js 段尾 MSE 缓冲洞、卡死和总时长缩水。

写入事务本身（stage → adjust → commit 的顺序与失败语义）属数据层规范，见
[DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)。

---

## 3. 媒体时间基：`mdhd.timescale` pin 死 90000

**tick 尺度必须是全轨常量，不能让 ffmpeg 按段自选。** `{track}_init.mp4` 只由该轨首段生成、被
整条 playlist 复用（`#EXT-X-MAP` 声明的就是它的时间基）；而不指定时 ffmpeg 按「fps 有理数约分
分子 × 2^k」自选 timescale——逐段 `eff_fps` 不同即逐段 timescale 不同，后续 fragment 的 tick
被按首段尺度解读，误差是**乘性**的。

- 实测：15fps 定 init、14.37fps 的段（自选 timescale=11496）→ 声明 10.02s 被读成 7.60s，
  单段 2.4s 空洞，hls.js 段尾停摆。
- 自选值对 fps **极不连续**（15.0→15360，但 14.37→11496，分子 1437 约不动），fps 抖 4% 可致
  timescale 差 25%——「fps 波动不大就没事」不成立。
- 取 **90000**（MPEG-TS/RTP 标准视频时钟）：整除 30/25/24/20/15/12/10 等常见帧率；非整除帧率下
  ffmpeg 按绝对 PTS 取整增量差分，误差有界 ≤ 半 tick（5.6µs）且不累积。
- 传法有坑：必须 `-hls_segment_options video_track_timescale=90000` 透传给**内层 mp4 muxer**，
  直接给 hls muxer 传 `-video_track_timescale` 会被**静默忽略**。

pin 之后 timescale 与编码 fps 彻底解耦，逐段 `eff_fps` 才是合法的速率表达；tfdt 累计偏移也因此
能按常量换算 tick，无需回读产物。

### tfdt 靠 hex-patch 而非 ffmpeg 参数

每个段由独立 ffmpeg 进程转码、输入自身从 PTS=0 起，不补偏移则所有 fragment 的落点都是 0，播到
第一段末尾就不前进。而 ffmpeg 8.x 的 HLS muxer + fmp4 在 `-start_number 0` 下**强制清零 tfdt**、
丢弃 `-output_ts_offset`（`-itsoffset`+`-copyts`、`-muxdelay` 同样无效），只能转码完直接改
`moof/traf/tfdt.baseMediaDecodeTime` 这个 box，写入「已入 playlist 的累计 EXTINF × 90000」。
box 结构固定、size 不变，是纯 metadata 改写；找不到 box / 版本装不下时**整段作废**，不放行一个
落点错的 fragment。

### 转码子进程必须 `cwd=输出目录` + 输出全传 basename

**两个 ffmpeg 大版本对 `-hls_fmp4_init_filename` 的路径解析行为正好相反**，唯一兼容写法是让子
进程 `cwd` 落在输出目录（现在是该段的 stage 目录）、所有输出参数只给文件名：

- ffmpeg 8.x（Windows 开发机）把 basename 解析到**进程 cwd**，传绝对路径才对；
- ffmpeg 4.x（Ubuntu 22.04 生产）把绝对路径**当相对路径**拼到 playlist 目录前，得到
  `/dir/foo/dir/foo/init.mp4` 这类路径 → ENOENT。

改成绝对路径「更稳妥」的直觉在这里是错的：一边对另一边就炸，且只在部署到另一个发行版时才暴露。

---

## 4. 墙钟↔媒体换算的唯一载体：`MediaTimeline`

`app/services/utils/media_timeline.py`。**媒体轴是压紧的墙钟**，所以「首段墙钟 + 媒体刻度」这个
换算只在从没断过流时成立，断过就偏早整整一个空洞。**换算要清单，只有这个模块有清单**——
调用侧（`/timeline` 的告警落点、`clip_builder` 的裁剪区间）一律经它，不自己凑。

### 落点怎么算

`MediaTimeline.load(task_id, step_id, track)` 读清单并展开成 `PlacedSegment(seg, media_start_ms)`
序列：**清单顺序即时序，累加 EXTINF 即落点**。累加用 float 秒、**只在出口取整到 ms**——逐段取整
再累加会让误差随段数线性累积（180 段最坏 90ms）。

刻度一律**相对整条轨的原点**，`select(start, end)` 取子集后**不重新归零**，否则调用方手上那个
绝对刻度没法直接相减。

`media_offset_ms(media_ms)` 是交给 ffmpeg `-ss` 的数：取子集时 ffmpeg 会把首段的 tfdt 归一到 0
（实测：绝对落点 100s 的子集照样得到 `start_time=0`），所以偏移是纯减法，不需要知道整条轨的原点。

### 跨模块前提：EXTINF 落盘精度是 ms 的整数倍

写侧 `app/storage/hls/_m3u8.py` 的 `entry()` 用 `:.3f`，于是每个 EXTINF 都是整毫秒，float64 累加
几百个这样的值在 ms 精度下不产生错位。**若哪天把精度提到六位小数，`load()` 要改成整数累加**，
否则段间出现 ±1ms 错位，表现是 `select()` 边界漏段、`media_offset_ms` 变负。

### 落进空洞的墙钟吸附到下一段的段首

`media_ms_at(wall_ms)` 把绝对墙钟换成媒体刻度。落进空洞时吸到**下一段的段首**：那段时间在媒体轴
上宽度为零，没有「对应刻度」可言，吸到下一段段首是唯一不撒谎的选择（不会声称某一帧拍于空洞之中）。
超出整条轨时贴到最近的一端；反向的 `wall_ms_at` 同样贴边，所以区间尾越过轨尾时产物会短，调用方
要把自己记的时长跟着收。

### 空洞判据必须留在墙钟轴上

```text
gap = 下一段起点 − (本段起点 + EXTINF) > GAP_THRESHOLD_MS
```

两项都是真值：起点是文件名里的实测墙钟，EXTINF 是清单里的段媒体时长。**fps 漂移天然被吸收**——
漂移同时压低 `eff_fps` 与段内帧数，`EXTINF = N/eff_fps` 跟着变长。

媒体轴上判不出来：它是压紧的，相邻段在它上面永远首尾相接，断没断过流都一样，空洞宽度恒为零。
段尾墙钟也**不能用「下一段起点」代替**，那会把停顿算进段长，而这个差值正是判空洞要的量。

### `GAP_THRESHOLD_MS = 500` 是保守护栏，不是精确判据

下界是正常段边界的残差（§1 的 `Δ̄ - gap_边界`，帧间隔量级，典型 ≈0、尾部几十 ms）；上界是能触发
重连的断流（decoder 进程退出 → 健康检查 1s 内发现 → respawn → RTSP 重连，秒量级）。0.5s 落在这
一到两个数量级的空当里，且**不随段长或 fps 变**——被它替换掉的「相邻段 ts 中位差 + 容差」判据正是
要跟着调的那种。精确判据要等 health_monitor 声明断点真值。

### 累计断流只有 `total_gap_ms()` 一个算法

它是所有超阈空隙之和。**别用「墙钟跨度 − Σ EXTINF」去凑**：那两个量不同尺——`/timeline` 的
`duration_ms` 是**双轨并集**的墙钟跨度，Σ EXTINF 是**单轨**的，差值里混着「两轨起止不对齐」这一项
（推理起步晚于取流时 processed 首段本就晚于 raw 首段），于是零断流的 step 也会算出假空洞。

### 媒体轴落点的三个同源表示，必须是同一个数

```text
写侧   tfdt            hex-patch 进 fragment 的 baseMediaDecodeTime ÷ 90000
浏览器 fragment.start  hls.js 解析清单后按 EXTINF 累加得出
后端   media_start_ms  MediaTimeline.load 按同样方式累加得出
```

三者岔开都不报错：进度条指针与画面错位、跳转跳到别处、裁剪裁到隔壁段。

---

## 5. 段枚举只认清单，域里不存在「不可播的段」

「有哪些段」过去有两个真源：文件系统枚举（`iterdir` + 文件名正则）与清单。前者把**在途段**
（mp4v 已落、transcode + append 未完成）和**登记失败的段**（段已就位、清单追加抛了 `OSError`）
一并算进来。喂给 ffmpeg 的后果是**测试全绿、日志无输出、产物看起来正常**：exit 0、任何日志级别
都无输出、`-xerror` 抓不住，产出结构完整、ffprobe 满意、能播、但**少一截**的 mp4
（见 [DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md)）。

现在文件系统枚举**已整个从域里删除**（不是降级为私有）：`hls.list_segments` 纯清单解析，一行条目
同时给出墙钟锚点（URI 里的 `ts_us`）与媒体长度（EXTINF），不跟文件系统 join；
`hls.list_segments_in_range` 同源同返回类型，只是多一次段级区间切片。收口后**「可播」不再是限定
词**：类型叫 `Segment`、枚举叫 `list_segments`（旧名 `PlayableSegment` / `list_playable_segments`
已不存在）。

**「在途段过滤」这个动作本身也消失了**：写侧改成在 `.stage_{track}_{ts_us}/` 暂存目录里编码转码、
commit 后才出现在正式位置，在途段窗口从根上没有了（原地编码会留下「文件在、但还是 mp4v 不是
fragment」的窗口，实测 ~260ms）。

清单顺序即时序（只追加），但 `list_segments` 仍显式按 `ts_us` 排一次并在**真出现逆序时记
warning**：写侧的 tfdt 是按清单顺序累加 EXTINF 算出来的，静默重排会让读侧算出的媒体偏移与文件里
的 tfdt 对不上，seek 到错帧且不报错。

---

## 6. init 段按轨分存，缺了返回 503

**按轨分存 `{track}_init.mp4`**（`raw_init.mp4` / `processed_init.mp4`）——raw 与 processed 是两条
独立 playlist、各有各的 `#EXT-X-MAP`，共用一个文件名会变成「谁先转码谁定」，另一条轨就指向别人的
init。init 只在该轨首次写入时落盘，已存在则丢弃新的那份（同轨同摄像头同编码参数，SPS/PPS 一致，
换新的没收益却会让已发布的清单换 init）。

缺 init 时回放（VOD playlist）与下载（step 导出）均返回 **503**，且**服务端无法自愈**：要么段是
分轨命名之前的旧格式产物（不支持，也不提供迁移路径），要么首段仍在 transcode 途中（窗口极短）。
用 503 而非 404 是刻意的——「此 step 不可回放」不是「资源不存在」。

---

## 7. 同一 step 的写与删必须同序

对同一 `{task_id}/{step_id}`，transcode、playlist append 和 metadata update 必须不重叠：相邻段的
tfdt 要读 playlist 求累计 EXTINF，两段并发进来会读到同一个累计值 → tfdt 碰撞 → 后段在播放器里覆盖
前段，**不报错、不卡顿，只是画面丢一截**。

**执行形态**：不再用目录锁，改由调用侧把同一 step 的写与删提交到同一条 `SerialTaskQueue`
（顺序是构造出来的，不是抢出来的），数据层一把锁都不持。删除是
`hls.delete(task_id, step_id)`（域粒度，只清 `{step}/hls/`）与 `tasks.delete_step`（跨域，整个
step 目录），两者都要与写同序。见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)。

---

## 8. 逐帧 ts sidecar（`.idx`）与离线帧反查

媒体轴是压紧的，段内第 k 帧的墙钟 ts 无法从 mp4 里问出来，而离线要按墙钟区间取帧。故 raw 轨每段配
一个同名 sidecar `raw_segment_{ts_us}.idx`：该段每帧 `frame.timestamp` 的 float64 **原值**数组，
一帧一条——无 magic、无长度字段、不依赖 `metadata.json`，条数由文件大小 ÷ 8 得出，`tofile(tmp)` +
`os.replace` 原子写、不持锁。**processed 轨不产**（渲染结果离线不消费），这条不对称是有意的——
因此解码入口 `hls.iter_frames` 连 `track` 参数都不设，恒为 raw。

**查询链路**：`ts 区间 → hls.list_segments_in_range 段级裁剪（清单解析）→ 段内读 sidecar
searchsorted 得帧号区间 → concat:{raw_init.mp4}|{seg.mp4} + select=between(n,k1,k2) + -vsync 0
解码 → 流式 yield Frame`。**不拼 m3u8、不用 `-ss`**（它按时间 seek，会让帧号原点漂掉），因此
**段内帧号与 sidecar 下标严格 1:1**；`select` 还必须排在 `scale` 之前，否则注定丢弃的帧也被缩放
一遍。每一帧都是「像素来自 mp4、时间来自 `.idx`」的合成物。

### 写读顺序：sidecar 必须先于段文件

commit 的顺序是 `sidecar → init → 段文件 → 清单条目 → 统计`，三条写反都不报错：

```text
sidecar 先于段文件     反过来留下「段可见但索引未就位」的窗口，离线反查此时拿不到 ts
init 先于清单头        清单头的 EXT-X-MAP 指着 init，指向不存在的文件 = 播放器直接报错
段文件先于清单条目     清单有行无文件 = 播放器直接报错
```

注意判据已随 §5 变化：**读侧判定「段存在」的唯一依据是清单里有条目**，不再是 mp4 文件存在。
残留的孤儿 `.idx`（mp4 写失败时）既不匹配段正则、也不在清单里，随 `hls.delete` 回收。

**两侧都要容错**：sidecar 写失败只 warning（不能拿主产物给辅助索引陪葬），历史段本就没有 `.idx`
——「有 mp4 无 idx」是合法状态。读侧缺 sidecar 时**跳过该段、不打断整条迭代**：缺一段的索引不该让
前后所有段一起读不了（该异常一抛会穿透 `yield from` 打死整个生成器）。

### ts 是帧的身份：位级精确，无容差

`FrameTracker.find(timestamps, width, height)` 要求传入 ts **位级等于** sidecar 中的帧 ts，任何精度
中转（float32、重新格式化）都抛 `ValueError`——这是设计而非缺陷：ts 应取自同一 run 的
`features.jsonl` / `FeatureStore.load()`，`json.dumps` 的 float repr + `float()` 回读是位级
round-trip，**配错帧比报错更坏**。产出顺序恒为 ts 升序，不保证与入参同序，调用方按
`frame.timestamp` 对号入座；重复 ts 按重数各产出一帧。

`FrameTracker` 的解码与裁剪已下沉到 `app.storage.hls`，它只剩位级配对；**只服务 raw 轨，已没有
`track` 参数**。

### 段级定位：区间起点数组要 `side="right") - 1`

清单里存的是**段起始** ts，查询问的是**哪个段包含** ts，两者差一个 `side` 方向——`"left"` 返回的是
ts **之后**的那个段。且段名 `ts_us = int(ts*1e6)` 是**截断**值（不是四舍五入，进位会让定位落到前
一段），连「ts 恰为该段首帧」这种可能侥幸的情形也被堵死，故用错方向是无条件错。同理，**段起始
数组推不出时间轴末端**（末段的段首之后还有整整一段的帧），默认区间的 `None` 必须一路下传到帧级
裁剪，由后者按「该侧不设限」处理。帧级的 `searchsorted` 返回天然自洽（空区间一律落到
`k_start > k_end`），刻意不 clamp——把 `k_end = -1` 救成 0 会把空区间误判成命中第 0 帧。

### 调用代价

顺序全量吞吐 ≈2000 fps（百倍于实时），单点反查 36–73ms 且随段内帧号线性增长（无 `-ss`、无关键帧
seek，要顺序解到那一帧）。**批量回看按 ts 排序后走 `hls.iter_frames(start_ts=…, end_ts=…)` 一次扫
过，别逐条 `find()`**——逐条查 N 个点 ≈ N 次整段解码。索引体积 8B/帧 ≈ 视频体积的 0.3%。

---

## 代码来源

- `app/storage/hls/`（写读两侧唯一真源）：`_encode`（eff_fps / mp4v）、`_fmp4`（转码 + tfdt
  hex-patch + `TIMESCALE`）、`_m3u8`（EXTINF 格式与累计）、`_idx`（sidecar 布局）、
  `_layout`（命名与 `ts_us` 截断）、`_read`（清单枚举）、`_write`（stage/commit 顺序）、
  `_decode`（解码与两级裁剪）、`types`（`Segment` / `SegmentRef`）
- `app/services/utils/media_timeline.py`（墙钟↔媒体换算、空洞判据、`GAP_THRESHOLD_MS`）
- `app/services/recording/service.py`（写与删的 `SerialTaskQueue` 编排）
- `app/routers/traceback.py`（`/timeline` 双坐标、VOD playlist 与缺 init 的 503）、
  `app/routers/media.py`（init / 段的 token 化分发）
- `app/services/lab/clip_builder.py`（`media_offset_ms` → `-ss`、跨空洞拒绝）、
  `app/services/lab/step_exporter.py`
- `app/services/inference/offline/frame_tracker.py`（位级配对；同文件的 `Timeline` 类已退役、
  零调用点，读的是旧平铺布局）
- `tests/test_media_timeline.py`、`tests/test_storage_hls.py`、`tests/test_lab_clip_builder.py`、
  `tests/test_traceback_router.py`、`tests/test_frame_tracker_boundary.py`、
  `integration_tests/test_frame_tracker_roundtrip.py`
