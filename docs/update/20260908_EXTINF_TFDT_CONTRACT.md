# EXTINF 与 tfdt 的分工契约：墙钟轴与媒体轴是两条轴，段间空隙只存在于前者

> **变更状态**：无代码变更（2026-09-08）　<!-- 纯知识梳理；据此识别出的缺陷与待验证项见「遗留风险」 -->
> **知识库**：已沉淀 → [DESIGN_HLS_TIMELINE.md](../kb/DESIGN_HLS_TIMELINE.md)（2026-09-20）

## 概述

梳理 HLS 落盘的两个时间量 —— playlist 里的 `EXTINF` 与 fragment 里的 `tfdt` —— 各自的定义、
算式、写入时机与分工，并给出由此推出的硬结论：**媒体时间轴是被压紧的墙钟，录制停顿在播放侧
不存在**。据此识别出前端一处正确性缺陷。无代码改动。

## 变更背景

- **现状 / 痛点**：这两个量的约束散在四处注释里（[`_effective_fps`](../../app/services/persistence/strategies/hls_strategy.py)、
  [`_ts_offset_seconds`](../../app/services/persistence/strategies/hls_strategy.py)、
  [`playlist.py`](../../app/services/step_store/playlist.py) 模块 docstring、
  [`_patch_fragment_tfdt`](../../app/services/persistence/strategies/hls_strategy.py)），
  每处都只讲自己那一半，**没有一处写全两者的分工**。结果是三类反复出现的困惑：谁是段时长的
  真值、为什么不能用文件名 ts 差重推、断流停顿去哪了。
- **触发来源**：clip_builder 收口到 EXTINF 的改造评审。评审中发现它自建了第三套段时长口径
  （相邻段 ts 跨度），而这套口径成立的前提在 2026-07-23 就已消失（见 §1）。
- **承接**：`eff_fps` 自适应反推由 `c61b174`（2026-07-23）引入，本文所有算式以该版本为准；
  在此之前 EXTINF 是 `帧数 / 假定 raw_fps` 的恒定值，本文结论对旧产物不成立。

## 正文

### 全景：三条时间线

同一批帧派生出三个时间量，各自服务于不同的问题：

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

**空隙只在①，②里不存在。** 段 2 的文件名从 110 跳到 140，但它的 EXTINF 照样是 10.000、
tfdt 照样接在段 1 后面。播放器只看②。

| 时间量 | 落在哪 | 回答什么问题 | 详见 |
|--------|--------|-------------|------|
| 文件名 `ts_us` | 段文件名 `{track}_segment_{ts_us}.mp4` | 「这段画面是什么时候拍的」——检索、定位、告警对号 | §1 |
| `EXTINF` | `{track}_playlist.m3u8` 文本行 | 「这段有多长」——播放器估算用 | §2 |
| `tfdt` | fragment 内 `moof/traf/tfdt` box | 「这段字节放在媒体轴哪里」——播放器落点用 | §3 |

两者的分工见 §4，写入时序与锁的约束见 §5，由此推出的播放侧结论见 §6，失效模式速查见 §7。

### 1. 文件名 `ts_us`：墙钟轴，且**不是**段时长

`ts_us = int(frames[0].timestamp × 1e6)`（[`layout.ts_to_us`](../../app/services/step_store/layout.py)），
即段内首帧的墙钟时刻截断到微秒。

**不能用相邻段的 `ts_us` 差当段时长。** 它等于「本段媒体时长 ± 一个帧间隔的抖动」，而且遇到
断流时会把整个停顿算进段长。历史上用它推 EXTINF 导致过 hls.js 段尾停摆与总时长缩水。

代数关系（`Δ̄` = 段内平均帧间隔）：

```text
span     = t_{N-1} - t_0        = (N-1)·Δ̄          段内首末帧跨度
EXTINF   = N / eff_fps          = N·Δ̄ = span + Δ̄   多出的 Δ̄ 是末帧自身的显示时长
ts 跨度  = t'_0 - t_0           = (N-1)·Δ̄ + gap_边界

EXTINF - ts 跨度 = Δ̄ - gap_边界
```

切段是**纯计数切**（[`HLSSegmentSweeper._sweep`](../../app/services/persistence/workers/segment_sweeper.py)
从连续缓冲里按 `ca_segment_len` 帧切走一批），所以第 N-1 帧与第 N 帧是同一路采集的相邻两帧，
`gap_边界` 与段内任意帧间隔**同分布**。于是该误差零均值、量级为一个帧间隔（15fps 下 ±67ms），
累计是随机游走而非单调漂移。唯一的例外是重连后的首段——那时 `gap_边界` 是真实停顿。

### 2. `EXTINF`：段时长真值，与编码帧率同源

```python
eff_fps          = (N-1) / (t_{N-1} - t_0)      # 落 [1,60] 带外或单帧 → 15.0
cv2.VideoWriter(..., eff_fps, ...)              # 用它编码
segment_duration = len(frames) / eff_fps        # 用它写 EXTINF
```

**同一个 `eff_fps` 同时喂 VideoWriter 与 EXTINF**，这是 EXTINF 成为真值的全部理由：cv2 用
`eff_fps` 写 N 帧，输出媒体时长就是 `N/eff_fps`；ffmpeg 转 fMP4 保持该时长。声明值与实际值
同源，不可能漂。

写入位置：[`_persist_raw_segment`](../../app/services/persistence/strategies/hls_strategy.py) /
`_persist_processed_segment`，以 `#EXTINF:{dur:.3f},\n{filename}\n` 追加。

> **退化段是唯一不准的情形**：`N=1` 或 `span<=0` 时 `eff_fps` 回落常量 15.0，EXTINF = 1/15
> 与该帧实际覆盖的墙钟时间无关。但它仍**自洽**（tfdt / mdhd / EXTINF 三者一致），播放不出洞，
> 只是墙钟↔媒体的换算在这一段失真。

### 3. `tfdt`：媒体轴落点，等于前面所有 EXTINF 之和

```text
tfdt(N) = Σ EXTINF(0..N-1) × 90000        单位 tick，timescale pin 死 90000
```

每个段由独立 ffmpeg 进程转码，输入 mp4v 自身从 PTS=0 起，不补偏移则所有 fragment 的 tfdt
都是 0。取值由 [`_ts_offset_seconds`](../../app/services/persistence/strategies/hls_strategy.py)
读同目录 playlist 求和得到——**调用发生在把本段 append 进 playlist 之前**，所以此刻 playlist
恰好只含 0..N-1 段，求和即得本段起点。首段读不到条目，返回 0.0。

写入方式是 **hex-patch**，不是 ffmpeg 参数：ffmpeg 8.x 的 HLS muxer + fmp4 在 `-start_number 0`
下强制清零 tfdt，`-output_ts_offset` / `-itsoffset+-copyts` / `-muxdelay` 全部无效。只能转码完
直接改 `moof/traf/tfdt.baseMediaDecodeTime`（[`_patch_fragment_tfdt`](../../app/services/persistence/strategies/hls_strategy.py)）。

`timescale` 必须 pin 死 90000 且传给**内层 mp4 muxer**（`-hls_segment_options video_track_timescale=90000`，
直接给 hls muxer 传 `-video_track_timescale` 会被静默忽略）。理由与实测数据见 KB
[DESIGN_HLS_TIMELINE.md](../kb/DESIGN_HLS_TIMELINE.md) 的「媒体时间基」一节。

### 4. 分工：EXTINF 管「估」，tfdt 管「放」

| | `EXTINF` | `tfdt` |
|---|---|---|
| 住在哪 | playlist 的文本行（段外） | fragment 字节内（段内） |
| 单位 | 秒，float 3 位小数 | tick，int，timescale=90000 |
| 值 | `len(frames) / eff_fps` | `Σ EXTINF(0..N-1) × 90000` |
| 谁写 | playlist append | 转码后 hex-patch |
| **播放器用它做什么** | **估**：`<video>.duration` 显示、seek 时该抓哪一段、缓冲与 TARGETDURATION 判断 | **放**：这段字节落在 MSE 时间轴的哪个位置 |
| 写错的表现 | seek 落点偏、总时长不对、TARGETDURATION 违规被播放器拒 | 覆盖 / 空洞 / 停摆 |

**播放器按 playlist 顺序「取」段，按 tfdt「放」段。** 段与段的连续不是播放器推断出来的行为，
而是写侧维持的不变式——它把 tfdt 恰好设成前面所有 EXTINF 之和，两段才严丝合缝。

### 5. 写入时序：三件事必须在同一把目录锁内

```text
锁外  ① _update_timeline        写 sidecar .idx（须早于 mp4，见 KB「写读顺序」）
锁外  ② cv2.VideoWriter         写 mp4v（此刻段文件已对读侧可见 = 在途段窗口开始）
锁外  ③ segment_duration = len(frames) / eff_fps

with _get_dir_lock(task_id, step_id):
  ④ playlist 不存在则写文件头（含 #EXT-X-MAP）
  ⑤ _transcode_to_fmp4_segment  ├ 读 playlist 求和 → ts_offset
                                 ├ ffmpeg mp4v → fMP4，os.replace 就地换掉 ②
                                 └ hex-patch tfdt = ts_offset × 90000
  ⑥ playlist append #EXTINF     ← 在途段窗口在此关闭
  ⑦ _update_metadata
```

**⑤⑥⑦ 必须原子**：⑤ 读的是「已入 playlist 的累计 EXTINF」，若相邻两段并发进入，两者会读到
同一个值 → tfdt 碰撞 → 后段覆盖前段。锁按 `(task_id, step_id)` 细分，留在写侧、不进
`step_store`（它出的是随手构造的无状态句柄，锁表挂上去每次拿到不同的锁、完全失效）。

**②→⑥ 之间是「在途段」窗口**（实测 ~260ms）：mp4 已在磁盘上、读侧扫得到，但它此刻还是 cv2
写的 mp4v 独立文件（带自己的 moov），不是 fMP4 fragment，也没有 EXTINF。喂给 HLS demuxer +
`EXT-X-MAP` 要么失败要么出垃圾。这就是 `Step.segments(playable_only=True)` 默认滤掉它的原因，
判据即「在不在 playlist 的键集合里」。

### 6. 推论：媒体轴是压紧的墙钟，播放器看不见段间空隙

由 §3 的 `tfdt(N) = Σ EXTINF(0..N-1)` 直接得到：段 N 的媒体起点 **恒等于** 段 N-1 的媒体终点。
媒体轴上不存在空隙，无论墙钟上发生了什么。

```text
currentTime = 19.9s  →  断流前最后一帧
currentTime = 20.0s  →  重连后第一帧      ← 画面跳变，时间轴零痕迹
```

`<video>.duration` = Σ EXTINF = 净媒体时长，比该 step 的墙钟跨度短一个 Σgap。

**这是刻意的，不是疏忽。** 反过来让 tfdt 按墙钟走，MSE 的 buffered 会变成
`[0,20) ∪ [50,60)`，hls.js 播到 20s 停摆、不会自己跨过去——正是
[HLS_TIMELINE_PITFALL.md](../archive/resolved_issues/HLS_TIMELINE_PITFALL.md) 记录的坑。
而 HLS 协议本身也没有表达「这里有一段时间没画面」的手段：`#EXT-X-DISCONTINUITY` 表达的是
**编码参数变了**（timescale / codec / PTS 基准重置），不是时间空白；playlist 的时间轴就是
EXTINF 累加出来的，结构上没有放洞的位置。

**所以段间空隙在播放层无解也不必解，只能在应用层处理**——即前端的
`墙钟 ts ↔ player.currentTime` 换算。

### 7. 失效模式速查

| 写坏了什么 | 播放器行为 | 是否报错 |
|-----------|-----------|---------|
| tfdt 全 0（漏 patch） | 所有 fragment 落在 `[0, EXTINF)` 互相覆盖，播到第一段末尾停住 | 否 |
| tfdt 之间留洞 | buffered 出现空洞，播到洞口停摆，不自己跨 | 否 |
| tfdt 之间重叠 | 后段覆盖前段尾部，画面丢一截 | 否 |
| tfdt ≠ 累计 EXTINF | seek 到 20s 出来的是 18s 的画面 | 否，静默错位 |
| EXTINF 用 ts 跨度而非 `N/eff_fps` | 声明时长与 fragment 实际媒体时长不符 → 段尾 MSE 缓冲洞、总时长缩水 | 否 |
| timescale 逐段自选（未 pin） | tick 被按首段尺度解读，误差乘性；实测声明 10.02s 被读成 7.60s | 否 |
| 在途段进了 playlist | fragment 是 mp4v 非 fMP4，demuxer 失败或出垃圾 | ffmpeg 报错 |

全部是静默失败——这也是 `step_store` 只出 m3u8 成品、不出骨架的原因：骨架写错播放器立刻报错，
备料（EXTINF 真值、滤在途、判 init、算 TARGETDURATION）写错才是静默的。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **前端墙钟↔媒体线性换算错位（P1）**：[lab/index.html](../../app/static/lab/index.html) 的 `currentAbsMs`(:573)、`seekToMs`(:704)、`markerStyle`(:693) 都按 `start_ms + currentTime` 直接线性换算，隐含「媒体轴 = 墙钟轴」。§6 表明该假设在断流重连后不成立 | 同一 run 内断流重连（`cleanup_timeout` 内重连不 purge 目录，直接续写）后：gap 之后的告警标记系统性错位、点标记跳转到错画面。run 重启走 supersede 整目录删，反而无此问题 | 待定方案 A：`/traceback/task/{id}/timeline` 增出 `media_spans`（墙钟段 → 媒体偏移），前端把全局线性映射换成分段线性。不动落盘格式、不动 tfdt、告警仍是墙钟真值 |
| **timeline 与 playlist 的时长口径不一致**：`/timeline` 的 `duration_ms` 取 `time_bounds_us`（墙钟跨度，含 gap），playlist / 导出 mp4 是 Σ EXTINF（净媒体） | 两者差一个 Σgap，是上一条错位的根因 | 同上；[docs/api/traceback.md](../api/traceback.md) 需同步补这条口径说明 |
| **clip_builder 自建第三套段时长口径**：往临时 m3u8 里写相邻段 ts 跨度而非 playlist EXTINF | 与本契约不一致；末段时长靠中位数估算 | 改造中，见门禁 [`PLAYLIST_ALLOWED`](../../tests/test_import_hygiene.py) 的具名例外与其退出条件 |
| **未验证：ffmpeg HLS demuxer 的 seek 基准**。从 step 中间截一段做临时 m3u8 时，fragment 的 tfdt 是「从 step 开头」的绝对值，而临时 playlist 的累计 EXTINF 从 0 起。`-ss` 落在哪条轴上，读代码定不了 | 若信 tfdt，则 clip 的 `-ss` 可能整个不生效（首段 tfdt 是个大数）；现状线上可用，倾向信临时 playlist，但未实测 | clip_builder 改造前做一次对拍：同一区间用两种 EXTINF 各构一次，解出输出首帧与 `Step.frames()` 的 sidecar 真值 ts 比对 |
