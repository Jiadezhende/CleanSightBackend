# 送标与进度条改用媒体坐标

> **变更状态**：已实现（2026-09-20）
> **选型依据**：[20260919_VIDEO_TIMEBASE_SELECTION.md](20260919_VIDEO_TIMEBASE_SELECTION.md)
> Q1 / Q3 / Q5 / Q6 / Q7（Q 编号是稳定锚点，该文 §6 有编号 → 章节对照表）。
> **本文实现的就是该文的选型结论**：UI 侧统一媒体轴，是当前需求下的采用项，不是降级方案。
> **知识库**：待沉淀
> **前置**：[20260920_SEGMENT_QUERY_VIA_PLAYLIST.md](20260920_SEGMENT_QUERY_VIA_PLAYLIST.md)
> （段查询收口到清单，已单独提交）

## 修的是什么

两处已在线上的 P1，共同点是**测试全绿、日志无输出、产物看起来正常**：

| 缺陷 | 触发 | 表现 |
|---|---|---|
| #1 送标 clip 早 Σgap | 断流后 | 前端按 `首段墙钟 + currentTime` 上报，而媒体轴是**压紧的**墙钟——断流那 20 秒在它上面不存在。裁出来的 clip 里没有操作员标的那个事件，时长对、能播、不报错 |
| #2 告警标记与播放头差 Σgap | 断流后 | 进度条全长用墙钟跨度、播放头用媒体 `currentTime`，两把不同尺的量混用。10 分钟含一次 20s 断流的 step，播到底指针只走到 96.7% |

同一批选型里的第三处（#3 裁剪吃进未登记段）已作为前置单独落地，见上方链接——本文的清单
算术全部建立在"段只从清单来、EXTINF 是时长真值"之上，那是前置给的。

**破坏式契约变更**：`/lab-f3m8/submit` 的区间字段由 `start_ms`/`end_ms`（绝对墙钟）换成
`start_media_ms`/`end_media_ms`（媒体刻度）。墙钟由后端换算，随响应带回。

## 1. 段查询收口到清单（已单独提交）

整节移到 [20260920_SEGMENT_QUERY_VIA_PLAYLIST.md](20260920_SEGMENT_QUERY_VIA_PLAYLIST.md)：
枚举器收口、`list_playable_segments` → `list_segments` / `PlayableSegment` → `Segment` 的
改名、traceback 404/503 先后对调、`lab.py` 的 `updated_time` 取段尾。本文后续各节直接用它的
结论，不再重复。


## 2. 送标改媒体坐标（修缺陷 #1）

**契约破坏式变更**（消费方只有 lab 页面一个）：

```text
请求   { start_media_ms, end_media_ms, label? }     前端唯一能诚实给出的量
响应   { start_media_ms, end_media_ms,              请求回显，失败时也有
         start_ms, end_ms, ... }                    后端换算的墙钟真值，算不出时 null
```

**响应必须带墙钟，不是冗余。** 选型文 §2.2 那条边界——媒体刻度是瞬时坐标、不是持久标识
——在这里落地：媒体刻度的含义依赖段集合（孤儿段补登记、过期段清理都会改变前缀和，同一个
`media_ms` 就指向另一帧），墙钟由采集那一刻决定，与盘上还剩哪些段无关。所以**离开本次请求
的时刻一律落墙钟**：LS meta 同时写两套（[`lab.py:516-525`](../../app/routers/lab.py#L516-L525)），
`start_ms`/`end_ms` 是可追溯的那一套，`start_media_ms`/`end_media_ms` 只是请求回显。

| 文件 | 改了什么 |
|---|---|
| **新增** [`services/utils/media_timeline.py`](../../app/services/utils/media_timeline.py) | `MediaTimeline`：段序列展开成媒体轴 + 墙钟↔媒体双向换算 + 空洞判据。两个消费方（`clip_builder`、`traceback`），故落在 utils 而不是任一 service |
| [`lab/clip_builder.py`](../../app/services/lab/clip_builder.py) | **整个重写**。区间改媒体坐标，选段/偏移变成清单上的纯减法；删掉 `_est_segment_duration_us` 与"墙钟 EXTINF"改写；`gap_tolerance_ms` / `default_segment_duration_s` 退役 |
| [`routers/lab.py`](../../app/routers/lab.py) | `LabClipRange` / `LabClipResultDTO` / `_validate_clips` / LS meta 跟着换 |
| [`static/lab/index.html`](../../app/static/lab/index.html) | `markStart/markEnd` 取 `currentMediaMs`；提交载荷换字段 |
| [`docs/api/lab.md`](../api/lab.md) | 三套时间口径的表 + 逐字段更新 |

**连续性判据换了输入**：`gap = 下一段起点 − (本段起点 + EXTINF) > 0.5s`。旧判据拿「该 step
全量段相邻间隔的中位数」当基准再留可配容差，那是**没有段时长真值**时的补偿；EXTINF 就是
真值。阈值语义见 `GAP_THRESHOLD_MS` 的注释——**它是保守护栏不是精确判据**，下界是帧间隔
量级的残差、上界是秒量级的真实断流，0.5s 落在中间且不随段长/fps 变。

#### 媒体轴落点的三个同源表示

段在媒体轴上的起点 = **此前所有 EXTINF 之和**。这个值在系统里有三处表示，**必须是同一个数**：

```text
写侧   tfdt              hex-patch 进 fragment 的 baseMediaDecodeTime ÷ 90000
浏览器 fragment.start    hls.js 解析清单后按 EXTINF 累加得出
后端   media_start_ms    MediaTimeline.load 按同样方式累加得出
```

三者同源，「前端报的媒体刻度能被后端翻译回同一帧」才成立。任一处换了累加口径（比如按
ts 排序后累加，而写侧按清单顺序累加），读侧算出的偏移就与文件里实际的 tfdt 对不上——
**seek 到错误的帧，且不报错**。`_read.list_segments` 的防御性排序因此带一条
warning：真出现顺序分歧时要让它可见，而不是静悄悄重排。

`MediaTimeline.load` 的累加用 float 秒、只在出口取整到 ms，依赖一个跨模块前提：**EXTINF
的落盘精度是 ms 的整数倍**（写侧 `_m3u8.entry()` 的 `:.3f`）。若哪天把精度提到六位小数，
这里会出现 ±1ms 的段间错位——`select()` 在边界漏段、`media_offset_ms` 变负。真要改精度，
累加得换成整数。


## 3. 进度条统一到媒体轴（修缺陷 #2）

| 文件 | 改了什么 |
|---|---|
| [`routers/traceback.py`](../../app/routers/traceback.py) | `/timeline` 增 `track` query（默认 raw）、`media_duration_ms`、每个事件的 `media_offset_ms` |
| [`static/lab/index.html`](../../app/static/lab/index.html) | 横轴分母换 `media_duration_ms`；`markerStyle` 吃 `media_offset_ms`；`seekToMs` → `seekToMediaMs`（媒体刻度直接就是 `currentTime`）；切轨重取 timeline |
| [`docs/api/traceback.md`](../api/traceback.md) | 两套坐标的分工表 |

**横轴取媒体轴是选型结论，不是妥协**（选型文 §2.2「横轴选型」）：硬约束只有"全长与播放头
同尺"，两条轴都能满足，差别是换算被推到哪一侧——媒体轴把换算推给后端（告警落点低频、可
预算、一次下发），墙钟轴把换算推给浏览器（播放头每个 `timeupdate` 一次）。墙钟轴唯一更强
的地方是空洞可见，而那不是当前需求。选型文初稿曾主张"保持墙钟横轴、只改播放头"，已按此更正。

**两处 UI 功能减项**：右侧"当前播放"与待选/已选区间改显示片内相对时间——今天那个墙钟是
**假值**，媒体轴下算不出真值。告警 tooltip 与提交结果表里的墙钟不受影响。

顺带修掉指针走不满：全长与播放头同分母之后，播到底就是 100%（原先 10 分钟含 20s 断流的
step 只走到 96.7%）。


---

## 4. 验收发现与修复（三路并行 review）

落地后按 A / B / C+D 三块各跑了一遍独立验收。**三条确认缺陷都是本次改动自己引入的**，
且全部属于"测试全绿也发现不了"的那类——记在这里是因为它们各自代表一种反复出现的形状。

| # | 缺陷 | 为什么测试发现不了 | 修法 |
|---|---|---|---|
| 1 | `_forget` 无差别清 `_pending_flush`，会吞掉**新一代**的挂起请求 | 要"`forget_task` 排队期间同一 task 重启且再次断流"才触发；单测里队列是同步的，这个窗口不存在 | 加对象身份核对（同 `take_pending_flush`）；顺带把 dict 推导换成 `list()` 快照——逐元素迭代不是原子的 |
| 2 | 前端"含信号中断"提示用 `duration_ms − media_duration_ms` 推断，**零断流也误报** | 那是两把不同尺的量：前者双轨并集墙钟跨度、后者单轨 Σ EXTINF。看 processed 轨时必然误报（推理起步晚于取流） | `/timeline` 增 `gap_total_ms` / `has_gap`，由后端按逐段精确判据算；前端只显示不推导 |
| 3 | 区间越过轨尾时 `duration_ms` 与 `end_ms − start_ms` 不自洽 | UI 侧越不过 `<video>.duration`，只有直调 API 才触发 | `duration_ms` 跟着 clamp |

**一条推翻了自己的判断**：`_pending_flush` 的 docstring 原写"SPSC，health_monitor 写、
sweeper 读"。实际有**三个**线程碰它（第三个是队列线程的 `_forget`），而漏掉的那个恰好就是
缺陷 1 的所在。免锁的结论侥幸成立，论证不成立——已改成按"`dict` 单次操作原子、逐元素迭代
不原子"来陈述。

另外补了一处**行为改善**与三处测试缺口：

- 重连成功时用**同一个栅栏**再登记一次 flush，捞走迟到的 processed 帧——那条属于
  [20260920_RECONNECT_RESIDUAL_FLUSH.md](20260920_RECONNECT_RESIDUAL_FLUSH.md)，记在那份里。
- 测试：顺序不变式补 processed 轨（只给 raw 时，把 `take_pending_flush` 挪到两个 `while`
  之间照样绿）；AST 门禁从 `ClipBuilder` 泛化到 `ClipSpec`（同样的暴露面，只堵一个等于留
  一个）；补 `gap_total_ms` 的零断流用例。


---

## 5. 本轮不做（未触发，非降级）

空洞可见性那一组整体未做：

```text
逐段 #EXT-X-PROGRAM-DATE-TIME 写入        空洞可见的唯一数据来源
前端墙钟横轴 + 空洞禁区绘制
/timeline 的 gaps 字段
跨空洞拒绝整段导出（StepExportHasGaps）
空洞真源选型（清单算术推断 vs health_monitor 声明）
```

选型文（§2.2、§6）把这一组的触发条件定成"审计要求录像中断必须可见"，当前不要求，所以
是**未触发**而不是欠账——本轮的取舍与选型结论一致，不是削减。

**边界**：含断流的录像导出后仍看起来连续、中间少 20 秒且无提示。页面上唯一的提示是标题栏那句"含信号中断"，由 `/timeline` 的
`gap_total_ms` 给出（§4 缺陷 2 修掉了原先的 `duration_ms − media_duration_ms` 推断）。

**触发时三件事一起翻**（选型文 §2.2）：横轴换墙钟、播放头改为每帧 media→wall 换算、清单补
逐段 PDT。它们是一个决定，不能分开取——横轴翻了播放头就必须本地换算，本地换算就需要
浏览器侧有映射表。本文 §2 的换算工作（`MediaTimeline` 的双向换算）届时可复用。


---

## 6. 遗留风险 / 后续任务

### 本轮接受的代价

> 断流 flush 那条线的残留（短停顿仍被吞、processed 轨只修一半、拆除期并发）记在
> [20260920_RECONNECT_RESIDUAL_FLUSH.md](20260920_RECONNECT_RESIDUAL_FLUSH.md) 里，不在本文重复。



| 项 | 影响 | 为什么接受 |
|---|---|---|
| **含断流的录像仍可导出，且看起来连续** | 中间少 20 秒无提示，汇报素材与整段下载都受影响 | 空洞可见性未触发（§5）。页面上唯一提示是标题栏"含信号中断"，由 `/timeline` 的 `gap_total_ms` 给出 |
| 前端不再显示"当前播放是几点" | 待选/已选区间改显示片内相对时间 | 那个值今天是**假的**；媒体轴下算不出真值。它是选型文里浏览器侧**唯一**需要本地映射表的高频量，恢复入口是逐段 PDT（选型文 §2.2） |
| 看 processed 轨时已选区间条被隐藏 | `clips[]` 存的是 raw 的媒体刻度，而进度条分母随轨变（两轨 Σ EXTINF 不同尺），画上去位置是错的 | 这正是"媒体刻度不是持久标识"那条边界（§2）的表现；宁可不画也不给一个对不上的视觉位置。打点按钮本就只在 raw 可用 |
| 新判据只有 0.5s 保守护栏 | 极短的真实断流可能漏判 | 精确判据要等空洞轮的断点真值。阈值不需要调参，理由见 `GAP_THRESHOLD_MS` |

### 推迟而非消除的风险

- **hls.js 的 PDT API 形态未验证**（`fragment.programDateTime` 的可用时机与精度）。本轮不接
  PDT 所以不涉及，但它没有被证伪——选型文 §2.2 把它标成未验证项，因为"播放器解析清单时
  顺带把逐段墙钟解出来"正是 PDT 相对自研 JSON 侧信道的主要优势。**翻到墙钟轴那一轮要先验
  这条**，不成立则两个载体只剩"不新增真源"一条差距。

### 仍然欠着的测试

**`/lab-f3m8/submit` 零行为测试。** 这一轮已经因此漏过一次：`gap_tolerance_ms` 退役时
`clip_builder` 删了参数、`lab.py` 漏改仍在传，805 条全绿而端点会 500。现在有一条 AST 门禁
（路由构造 `ClipBuilder` / `ClipSpec` 的 kwarg 必须都在签名里），但**门禁不等于行为测试**。
补一条的成本不高：替身只要 ffmpeg 与 `LabelStudioClient` 两个。

同类缺口：`/task/history`、`/lab-f3m8/tasks` 没有"盘上有段但未登记"的路由级用例；
`TestStepSummaryRecipe._summarise` 是 `routers/task.py._summarise_steps` 的手抄副本，
路由侧漂移它发现不了。

### 后续任务

| 待办 | 触发条件 |
|---|---|
| 空洞可见性整套（验 hls.js PDT API → 逐段 PDT → 墙钟横轴 → 禁区 → 跨空洞拒绝出片） | 巡检审计要求"录像中断必须可见"时。三件事一起翻，见 §5 |
| 空洞真源改声明式（health_monitor 记断点，而非下游用算术重建） | 与上一条同批。它能去掉 0.5s 阈值。选型文 §2.2 指出"没有实时回放需求"使 step 封档时重排清单成为可能，声明式真值可以写在那个窗口 |
| 孤儿段补登记自检工具 | 未登记段实际频发时。**不是**退回双真源 |

---

> **知识库沉淀**：本文标「待沉淀 → `DESIGN_HLS_TIMELINE.md`」。本文全篇是落地记录，
> 描述的就是现状；**PDT 未实现**，它只出现在 §5 的未触发清单里。沉淀时把
> "UI 侧统一媒体轴、持久化落墙钟"写成现行设计，把墙钟横轴那一组写成条件触发的备选，
> 别写成待办欠账。
