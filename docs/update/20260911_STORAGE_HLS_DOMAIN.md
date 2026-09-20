# hls 域落地：一条 `insert_segment` 吞下从内存帧到可播段的全流程，零调用点改动

> **变更状态**：生效中（2026-09-11）　<!-- 新子包已落地并有 74 条单测覆盖；本期刻意不接调用点，`hls_strategy` 原样保留，运行时行为零变化 -->
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[DESIGN_HLS_TIMELINE.md](../kb/DESIGN_HLS_TIMELINE.md)（2026-09-20）
>
> **追加（2026-09-11，随 [RECORDING_SERVICE](20260911_RECORDING_SERVICE.md) 一起做的三处）**：
> ① `_insert.py` 改名 **`_write.py`**，新增域粒度删除 `delete(task_id, step_id)`——两个都是
> 本域的对外写侧动作，一个文件放得下（facade 对外从 9 个名字变 10 个）；
> ② `_layout._domain_root` 改名 **`domain_dir`**（去前导下划线，供同包的 `_write.delete`
> 用，样板注记已回填 `_root.py`）；
> ③ `_fmp4._TIMEOUT_S` **120 → 15 秒**——调用侧定为单写者后，一个卡住的 ffmpeg 堵的是所有
> task 的录制。正文里出现的 `_insert.py` 与 120 s 是当时的原文，未回改。
>
> <!-- 分期改造的第 2 期，全景与期次划分见 20260909_STORAGE_LAYER_BASE.md -->

## 概述

新建 `app/storage/hls/` 子包，把 HLS 落盘从「cv2 编码 → ffmpeg 转 fMP4 → tfdt 修补 →
sidecar / init / playlist / metadata 登记」整条流程收成**一个领域方法**
`insert_segment(task_id, step_id, track, frames)`：调用方交出内存帧序列、拿回段的身份键
`SegmentRef`，中间七步一步都不出现在签名上。**不迁移任何调用点**——
`persistence/strategies/hls_strategy.py` 原样保留、仍是当前生产路径，故运行时行为零变化。

## 变更背景

### 现状 / 痛点

`hls_strategy.py` 745 行里，落盘格式知识与持久化策略搅在一起：`_persist_raw_segment` 与
`_persist_processed_segment` 是两份 95% 相同的代码（差别只在"raw 多写一个 sidecar"），
而 `eff_fps` → `VideoWriter` / `EXTINF` / `tfdt` 这条**三值同源**的约束靠四处注释互相守。

三个具体问题：

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | **在途段窗口 ~260 ms**：cv2 就地写出的 mp4v 已叫 `{track}_segment_{ts}.mp4`，读侧扫得到，但它此刻还不是 fMP4 fragment | 读侧必须靠「在不在 playlist 键集合里」二次过滤；漏滤过就是 clip 静默截短（缺陷 #3） |
| #2 | **转码失败仍然登记**：`_transcode_to_fmp4_segment` 失败只打 warning、保留 mp4v，随后 playlist append 照常执行 | 清单里多一条喂给 demuxer 会出垃圾的条目，静默 |
| #3 | 两个 persist 函数各写一遍 playlist 头、EXTINF 行、metadata 调用 | 改一处忘另一处不会红 |

### 触发来源

[20260909 storage 分层](20260909_STORAGE_LAYER_BASE.md) 的第 2 期。该记录的 §7 规范在设计
本域时完成判据换代——storage 从「名字与定位」变成**数据层**，编解码、外部工具子进程随之
从「不进包」变成本层本职。本期是那套判据的第一个重域实例。

### 承接

- 落盘根、域名白名单、逐级定位来自第 1 期的 `app/storage/_root.py`，本域按约定在
  `_layout.py` 顶部声明一次 `_DOMAIN = "hls"`，域内所有路径经私有 `_domain_root()`。
- EXTINF / tfdt 的算式与分工取自 [20260908 契约梳理](20260908_EXTINF_TFDT_CONTRACT.md)，
  本期把那四处分散的注释收成代码里的一条直线（见下方全景）。
- **并发方案已换代**：原规范 §7.4 要本层自己持 per-`(task, step)` 锁（`_locks.py`），
  该模块已随 [`app/utils/task_queue.py`](../../app/utils/task_queue.py) 的
  `SerialTaskQueue` 落地而删除。顺序改由**提交序构造**，本域一把锁都不持，见 §4。

## 方案详情

### 全景：一次 insert = stage → adjust → commit

```text
Sequence[Frame]
   │
   │  ① stage（产物全在 {step}/hls/.stage_{track}_{ts_us}/ 里，读侧看不见）
   ├──▶ eff_fps = (N-1)/span，带外或单帧回落 15.0            _encode
   ├──▶ cv2.VideoWriter(source.mp4, eff_fps)                 _encode
   └──▶ ffmpeg -hls_segment_type fmp4 → init.mp4 + fragment_0.mp4   _fmp4
   │
   │  ② adjust（位置相关的最后修补）
   ├──▶ offset = Σ 既有 EXTINF（此刻清单只含 0..N-1 段）      _m3u8
   └──▶ hex-patch fragment 的 moof/traf/tfdt = offset × 90000 _fmp4
   │
   │  ③ commit（顺序即 W8，三条写反都不报错）
   ├──▶ raw_segment_{ts}.idx      索引先于主产物可见           _idx
   ├──▶ {track}_init.mp4          init 先于引用它的清单头       （首段才装）
   ├──▶ {track}_playlist.m3u8     头（含 EXT-X-MAP）           _m3u8
   ├──▶ {track}_segment_{ts}.mp4  os.replace 原子换名          ← 段在此刻才可见
   ├──▶ #EXTINF 行                主产物先于清单条目            _m3u8
   └──▶ metadata.json             统计，读改写整份             _meta
   │
   ▼  SegmentRef(track, ts_us)

任一步异常 → rmtree stage、不 rename、不登记，原异常上抛
```

**硬约束三条**（全在主干上，不是某一步的细节）：

1. **`eff_fps` 一个值喂三处**：编码帧率、EXTINF、（经 EXTINF 累加的）tfdt。写岔任何一个
   都不报错，表现是 hls.js 段尾停摆或总时长缩水。它算在层内，正是为了让这三个值不必
   分三条路跨边界传。
2. **② 必须早于清单 append**：offset 读的是"已登记的累计 EXTINF"，本段条目一旦先进去，
   求和就把自己也算进去了，tfdt 整体后移一段。
3. **段文件名一出现就是合法产物**：所以编码不能就地做。`.stage_` 开头既不匹配段正则也
   不是文件，读侧枚举天然跳过——#1 那个 260 ms 窗口由此消失。

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| 事务编排（stage / adjust / commit、失败作废） | `app/storage/hls/_insert.py` | §1 |
| 域根 / 文件名 / `SegmentRef` / stage 目录 | `app/storage/hls/_layout.py` | §2 |
| eff_fps 反推 + cv2 写 mp4v | `app/storage/hls/_encode.py` | §3 |
| ffmpeg 转 fMP4 + box 遍历 + tfdt hex-patch | `app/storage/hls/_fmp4.py` | §3 |
| 清单文本：头 / 条目 / 累计 EXTINF | `app/storage/hls/_m3u8.py` | §3 |
| sidecar float64 布局 / metadata.json 读改写 | `app/storage/hls/_idx.py`、`_meta.py` | §3 |
| 对外 facade（一个写入口 + 一组定位函数） | `app/storage/hls/__init__.py` | §2 |
| 并发前提（本域不持锁） | —— | §4 |
| 导入预算逐模块登记 | `tests/test_import_hygiene.py` | §5 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **一个 `insert_segment` + 域内私有模块（采用）** | 新包 7 个模块约 620 行（含大段契约注释）；调用点迁移推到第 4 期 | 采用。"像 INSERT 一样"是这次要的形状：调用方不该知道段是先编码还是先建清单 |
| 对外出 `encode()` / `transcode()` / `append_playlist()` 三件套 | 调用方拿到的是三个可以乱序调用的动作 | 否。三值同源与 W8 的顺序约束会重新变成调用方的义务，而写错全是静默的 |
| 按 track 拆 `insert_raw` / `insert_processed` | 两份 95% 相同的代码，正是现状 | 否。差别只有"raw 多一个 sidecar"一行，参数能表达（设计约束 5） |
| 连读侧（枚举 / VOD 清单）一起迁 | 本期 diff 翻倍，且读侧要同时改 6 个 router 的构造样板 | 否。写侧自成闭环，读侧随第 4 期与 `segment_finder` 的下线一起做 |

### 1. `_insert.py` — 事务编排（全景 ①②③）

对外唯一的写入口。签名只有身份键 + 货币：

```python
def insert_segment(task_id: int, step_id: int, track: str, frames: Sequence[Frame]) -> SegmentRef
```

**`track` 不给默认值**：写错轨不会报错，只是回放时两条轨的画面串了（两轨各自独立、
都合法）。**空 `frames` 抛 `ValueError`** 而不是返回 False——它不是"没事发生"，是调用方
算错了批次；何况要返回一个不存在的段的身份，本就写不出来。

**失败语义分三档**（规范 §7.0：层只答"环境坏了还是数据坏了"，重试几次是调用方的策略）：

| 失败 | 处置 | 理由 |
|------|------|------|
| 编码 / 转码 / 落盘失败 | 删 stage、原异常上抛（cv2 的失败翻译成 `OSError`） | W4。层不出领域异常，`PersistenceError` 由第 4 期的调用方包 |
| **tfdt 改写失败** | **整段作废**，抛 `RuntimeError` | 见下 |
| sidecar 写失败 | warning，段照常登记 | 它只服务离线反查；读侧本就容忍缺 sidecar。拿整段视频给辅助索引陪葬是坏交换 |

> **tfdt 改写失败为什么是致命的**：一个 tfdt 没修好的 fragment 进了清单，播放器会把它落在
> 媒体轴原点、覆盖前面的段——不报错、不卡顿，只是画面丢一截。它只会在 fragment 结构与
> 预期不符时发生（ffmpeg 换代），那是**系统性**问题，压着不说会静默烂掉整批录像。宁可
> 整段作废、让它在日志里喊出来。**这是本期唯一一处行为与现状不同**，见「遗留风险」。

**W7 的 stage 命名**：`.stage_{track}_{ts_us}` 与产物同键、不用随机 nonce——每段最多留
一份残留，重试自然复用同一个（入口 `rmtree` 一次即幂等），且目录名本身就说明"哪一份
产物没写完"。

### 2. `_layout.py` + facade — 身份键是 `SegmentRef`，不是散标量

```python
class SegmentRef(NamedTuple):
    track: str      # "raw" | "processed"
    ts_us: int      # int(首帧 ts × 1e6)，截断
```

**不带 `task_id` / `step_id`**（L3）：那两个是路由键、调用方手里本来就有；track 与 ts_us
是从文件名里解出来的，不放进来就得让调用方再解一次。读写两侧共用同一组定位函数——写侧
自己构造 ref，读侧经 `parse_segment_name` 解出 ref，**路径一律由结构重建**，外部字符串
从不进入路径拼接，path traversal 结构上不可能（L2，替掉第 1 期砍掉的 `contained_path()`）。

**`ts_to_us` 是截断不是四舍五入**：读侧段级定位的 `bisect_right - 1` 建立在"段名 ts ≤
段内首帧 ts"之上，进位会让它落到前一段。往返断言因此只在 **us 域**闭合（T2）。

facade 对外 9 个名字：`insert_segment` + `SegmentRef` / `TRACKS` / `ts_to_us` /
`parse_segment_name` / `segment_path` / `sidecar_path` / `init_path` / `playlist_path`。
`metadata_path` 与 `segment_name` 刻意不出——目前零外部消费方。

### 3. 四个格式模块 — 每个只认自己那一种字节

| 模块 | 管什么 | 从 `hls_strategy` 搬过来时的唯一实质改动 |
|------|--------|------------------------------------------|
| `_encode.py` | eff_fps 反推、mp4v 编码 | 加 `writer.isOpened()` 检查：cv2 打不开编码器**不抛异常**，后续 write 全部静默丢弃、留一个 0 字节文件 |
| `_fmp4.py` | ffmpeg 命令、ISO BMFF box 遍历、tfdt hex-patch | 失败从「warning + 保留 mp4v」改为**抛**（见 §1）；`patch_tfdt` 的读写失败照抛 `OSError`，只有"结构与预期不符"返回 False |
| `_m3u8.py` | 清单头 / EXTINF 行 / 累计求和 | 合并了两个 persist 函数里各写一遍的头与 append |
| `_idx.py` / `_meta.py` | sidecar 的 float64 裸数组 / metadata.json | metadata 改为 tmp + `os.replace`（路线 C）；解析不出时重建 + warning 而非抛 |

ffmpeg 那串参数一个字没动，包括三处踩坑注释（`-hls_segment_options
video_track_timescale=` 必须透传给内层 mp4 muxer、cwd + basename 兼容 ffmpeg 4.x/8.x 相反
的路径解析、`-hls_segment_filename` 必须含 `%d`）。`TIMESCALE = 90000` 与
`_DEGENERATE_FALLBACK_FPS = 15.0` 同样原样保留。

### 4. 并发：本域不持锁，顺序由 `SerialTaskQueue` 构造

**前提**：同一 `(task, step, track)` 的 `insert_segment` 串行调用，且与该 step 的
`purge_step` 同序——即提交到同一条 [`SerialTaskQueue`](../../app/utils/task_queue.py)。

破了这条前提：两段并发会读到同一个累计 EXTINF → tfdt 碰撞 → 后段在播放器里覆盖前段，
**不报错、不卡顿，只是画面丢一截**。

规范 §7.4 C1 原本要本层自己持 step 锁（`app/storage/_locks.py`），该模块已随队列方案落地
而删除。换代理由：顺序由**提交序构造**比抢锁更强——它顺带保证「旧残段先落盘、再整个
删掉」这条现在只活在 `run_control.py` 三行注释里的约束。代价是这个不变式落在层外、
门禁抓不到，故各域写入口的 docstring 必须写明它依赖这个前提（`app/storage/__init__.py`
的设计约束 8 已同步改写）。

### 5. 保留项（刻意不改）

- **`app/services/persistence/strategies/hls_strategy.py` 原样保留**，仍是当前生产写侧。
  两份并存是自觉的临时状态，随第 4 期迁移一并删。
- **读侧全部不动**：`traceback/segment_finder.py`、各 router 的 VOD 清单构造、
  `inference/offline` 的段解码都还在原处，本域目前只出写入与定位。
- **落盘目录仍是两套**：`hls_strategy` 写 `{step}/` 平铺，本域写 `{step}/hls/`。切换发生在
  第 4 期那一刻（见「遗留风险」）。

## 变更效果

| 维度 | 变更前（`hls_strategy`） | 变更后（`storage.hls`，第 4 期接上才生效） |
|------|--------------------------|--------------------------------------------|
| 写侧对外形态 | `persist_segment(task_id, step_id, segment_type, frames) -> bool` + 类实例 + 注入 `db_dir` | `insert_segment(task_id, step_id, track, frames) -> SegmentRef`，模块函数，无句柄 |
| raw / processed | 两个 95% 相同的函数 | 一个函数 + 一个 `track` 参数，差别只剩 sidecar 一行 |
| 在途段窗口 | ~260 ms（段文件已可见但还是 mp4v） | **不存在**：产物在 `.stage_*/` 里造，`os.replace` 后才可见 |
| 转码失败 | warning + 保留 mp4v + **照样进清单** | 整段作废，异常上抛，清单不动 |
| tfdt 改写失败 | warning + **照样进清单**（落在媒体轴原点、覆盖前段） | 整段作废，`RuntimeError` |
| cv2 打不开编码器 | 静默产出 0 字节段 | `OSError`，段不落盘 |
| metadata.json 写入 | 直接覆盖写（中断留半份） | tmp + `os.replace`；解析不出时重建而非抛 |
| 落盘位置 | `{step}/` 平铺 | `{step}/hls/`，域隔离 |
| 并发 | 类内 `_dir_locks` 按 target_dir 索引 + `release_dir_locks` | 层内零锁，顺序由 `SerialTaskQueue` 提交序构造 |
| `import app.storage.hls` | —（不存在） | 重依赖集合为空（cv2 在函数体内），逐模块登记预算 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_storage_hls.py`（新增） | **69 passed**，0.5 s ——段名/ts/sidecar/EXTINF 四组往返（T1/T2）、`parse_segment_name` 十种非法输入（L2）、产物落位与 step 根无文件、tfdt 改写四档（含 v0 溢出拒改）、eff_fps 四种退化、事务不变式九条（失败作废 / 无 stage 残留 / 重试复用同键 / sidecar 失败不沉船 / tfdt 失败整段作废 / 两轨不碰撞）、墙钟断流不在媒体轴留空隙 |
| 端到端（T4，真 cv2 + 真 ffmpeg） | 2 passed —— 连写两段，断言产物是 `styp+moof` fragment 而非整块 mp4、init 是 `ftyp`、`tfdt(1) == EXTINF(0) × 90000`、sidecar 与帧一一对应；缺 ffmpeg 时 skip |
| `tests/test_import_hygiene.py` | 通过，新增 8 条逐模块预算；cv2 塞回模块级会当场红 |
| 全量 `pytest tests/` | **602 passed**，零 failed（`--ignore=tests/test_storage_locks.py`，理由见下） |
| 运行时行为 | 未验证也无需验证——本期不改任何生产调用路径 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| ~~**`tests/test_storage_locks.py` 已失效，挡住全量收集**~~ | `app/storage/_locks.py` 随队列方案删除，该测试文件仍在，`pytest tests/` 直接 collection error | **已解决（2026-09-11）**：经人确认后删除该文件（166 行、11 条用例，唯一的 app import 就是那个不存在的 `_locks`；文件从未进过 git）。`pytest tests/` 现在无需 `--ignore` |
| **`insert_segment` 与 `SerialTaskQueue` docstring 里的 `write_segment` 命名不一致** | 仅文档不一致，无功能影响 | 队列那份示例改成 `insert_segment`，或反过来定名——第 4 期接线前拍板 |
| **转码 / tfdt 失败从「降级保留」改成「整段作废」** | ffmpeg 若因换代系统性失败，现状是回放静默烂掉、新行为是整批录像丢失但日志喊得很响 | 有意选择（静默错比丢数据更难发现）。**第 4 期接线时必须复核**：那一刻才真正生效，且届时可结合 `GuardedExecutor` 的重试次数评估 |
| **本期代码无生产消费方** | 新子包是死代码，只有单测在跑 | 分期的自觉代价。第 4 期是价值兑现点 |
| **落盘位置两套并存** | `hls_strategy` 写平铺、本域写 `{step}/hls/`，切换是 breaking 变更、无迁移路径 | 与第 1 期同一立场：旧落盘不做兼容，第 4 期切换前需人拍板**清空 `database/`** 的时机 |
| **读侧未迁入，层只会写不会读** | `T1` 的往返在"段"这一档闭合不了（写进去 `Sequence[Frame]`，读不回 `Frame`） | 规范 §7.8 那条未决项原样悬着。第 4 期迁枚举与 VOD 清单，段解码是否进层仍待拍板 |
| **`metadata.json` 的 `end_time` 恒为 `null`** | 照搬现状；没有写侧知道 step 何时结束 | 保留字段是因为读侧还在按这个形状解析。第 6 期 TTL 判据换代时一并处置 |
| **并发前提门禁抓不到** | 调用方若不走同一条队列，tfdt 碰撞是静默的 | 只能靠 review + 写进 docstring。第 4 期接线的 PR 需在描述里写明段写与 purge 提交到同一条队列 |
| **`#EXT-X-TARGETDURATION:10` 是硬编码** | 段长由上游按帧数切，真超过 10 s 就是协议违规，播放器可能拒 | 照搬现状，未见实际触发。真要修得先决定"谁知道段长上限"，归第 6 期 |

---

# 追加（2026-09-11）：读向对称 —— 解码与枚举进域

> **变更状态**：生效中　<!-- 纯增量：新增 `_decode.py` + `_layout.list_segments`，一处既有调用点都不改 -->

## 结论先行

`hls.read_segment` 落地，与 `insert_segment` 互为逆运算——**规范 §7.8 那条「T1 的往返在
段这一档永远闭合不了」就此拍板并兑现**：交出去的 `Sequence[Frame]`，能原样读回来
（ts 位级相等、像素逐帧对得上）。

**本期仍是纯增量**：`inference/offline` 的 `Timeline`、`traceback/segment_finder` 全部
原样不动，运行时行为零变化。域内新能力与它们两份并存，等写侧切到 `{step}/hls/` 之后
再搬调用点。

## 为什么这条能力必须在域内

`Timeline` 住在 `inference/offline`，却消费三样**写侧建立的**域知识：

1. `.idx` 路径靠 `with_suffix(".idx")` 自己拼（写侧另有一套拼法，两份实现）；
2. init 文件名 `raw_init.mp4` 自己拼；
3. **「段内帧号 = sidecar 下标」**——这条契约由写侧「逐帧写 ts + cv2 写 N 帧」建立，
   却只由读侧一段 docstring 承诺。

第 3 条是真正的理由。它的成立前提是解码命令必须 `concat:init|seg` + `select=between(n,…)`
且**不用 `-ss`**。谁哪天加个 `-ss` 优化，帧号整体平移——**不报错**，反查回来的是错帧，
而 `FrameTracker.find` 的位级 ts 比较会把它当「没找到」抛 `ValueError`，错因指向完全
错误的方向。写侧与读侧隔着一个包互相承诺这件事，是典型的静默失败温床。

## 三条拍板的决策

| 决策 | 选定 | 理由 |
|------|------|------|
| 落盘布局 | **只读 `{step}/hls/`**，不兼容 `{step}/` 平铺 | 加回退等于把「旧布局」永久焊进域里，第 4 期切完也删不掉。本期零风险：新代码没有调用方，平铺数据继续由原封不动的 `Timeline` 服务 |
| 出口粒度 | `read_segment`（单段）+ `iter_frames`（区间） | 前者是 `insert_segment` 的逆运算、往返测试的落点；后者收敛段级裁剪那套截断陷阱。逐段构造视觉特征走前者 |
| `find` 归属 | **位级 ts 匹配永不进层** | 「ts 是帧的身份，配错帧比报错更坏」是业务语义，按四问第四条不进。`FrameTracker` 本期整体不动 |

## 解码只服务 raw 轨（不是遗漏，是契约）

`processed` **不落 sidecar** 是既有的有意不对称（`_idx` / `_write` 都有账）：它是画完框的
渲染结果，离线反查要的是原始帧，拿渲染结果构造视觉特征等于把模型自己的输出再喂回去。

故域内解码**结构上只认 raw**：

- `iter_frames` **不设 `track` 参数**——给了就得回答「processed 传进来怎么办」，而正确
  答案是「不该问」；
- `read_segment` 收到非 raw 的 ref 直接 **`ValueError`**，不是静默返回空。现 `Timeline`
  对 processed 的表现是「sidecar 为空 → 静默产出零帧」，看起来像"这段没数据"，实际是
  "这条路不通"，两者必须分得开；
- `list_segments(task_id, step_id, track)` **保留 track 参数**：枚举是双轨合法的能力
  （VOD 清单两轨都要），与解码不同。

> 顺带修正一个误判：现 `_build_cmd` 把 init 写死成 `raw_init.mp4` **不是 bug**，是同一条
> 契约的旧表达。域内新实现把它从"碰巧写死"变成"显式约束 + 拒绝非法轨"。

## 域内零类、零句柄

`Timeline` 的实例状态全部化解，域内不重建任何类（设计约束 4）：

| `Timeline` 的实例字段 | 化解为 |
|---|---|
| `self._segs` | `list_segments()` 的返回值，调用方自己持有——逐段构造特征的场景本来就要它 |
| `self._timestamps` | `iter_frames` 内的局部变量 |
| `self._step_dir` | `_layout.domain_dir(task_id, step_id)` |
| `self._ffmpeg_bin` | 函数体内 `settings.ffmpeg_path`，同 `_fmp4` |

`_load_sidecar` 整个不复制——域内已有 `_idx.read`，语义完全一致（缺文件返空数组、读侧
跳过该段）。这是本次最直接的一处去重。

副作用是测试 seam 也跟着干净了：从 `FakeDecodeTimeline` 子类覆写，变成 monkeypatch
模块级 `_decode._run_ffmpeg`（域内唯一的解码 I/O 边界）。

## 签名上的刻意选择

```python
read_segment(task_id, step_id, ref, *, width, height, start_ts=None, end_ts=None) -> Iterator[Frame]
iter_frames(task_id, step_id,      *, width, height, start_ts=None, end_ts=None) -> Iterator[Frame]
```

- **`width` / `height` 无默认值**（`Timeline.iter` 是 `640/480`）。设计约束 5：漏传该是
  `TypeError`，不是静默产出一个尺寸——那会在下游变成 train-serve skew，是查不出来的那一档错。
- **无 `timeout` 参数**。解码预算是域内常量，口径同 `_fmp4._TIMEOUT_S`：
  `max(30s, 帧数 × 0.2s)`。
- **返回 `Iterator` 不是 `List`**。「一段占多少内存」是调用方的策略（640×480×3 × 150 帧
  ≈ 138 MB），要整段进内存自己 `list()`。
- **`read_segment` 自己不是生成器**（只把 `_run_ffmpeg` 的生成器返回出去）：轨道校验要在
  调用那一行就炸，不能潜伏到循环深处。测试 `test_read_segment_rejects_processed` 刻意
  不写 `list()`，改成 `def ... yield` 会让它红。

## 自测结果

| 项 | 结果 |
|----|------|
| `tests/test_storage_hls.py` | **104 passed**（新增 30 条）——段枚举 5 条、段级裁剪 8 条、帧级裁剪 4 条、轨道契约 4 条、解码命令 2 条、真 ffmpeg 往返 5 条 |
| 段级往返（真 cv2 + 真 ffmpeg） | ✅ 帧内色块编码 frame_id（BGR 各 4 bit、阶距 17），写进去读回来：**ts 位级相等 + 像素 id 逐帧匹配**。实测压缩漂移 3/255，远在 8.5 容差内 |
| 解码命令契约 | ✅ 断言无 `-ss`、`concat:` 拼 init、`select` 排在 `scale` 之前、按帧号而非时间选 |
| 帧可写性 | ✅ 逐帧新建 `bytearray`：返回的帧可原地改，且改一帧不污染另一帧 |
| `tests/test_import_hygiene.py` | 24 passed，新增 `app.storage.hls._decode` 预算行；重依赖集合为空（解码走 ffmpeg 管道，不碰 cv2） |
| **上游未破坏** | `tests/test_frame_tracker_boundary.py` **24 passed**；`integration_tests/test_frame_tracker_roundtrip.py` **13/13 PASS**——都走完全未改动的 `Timeline` + 平铺布局代码路径 |
| 全量 `pytest tests/` | **671 passed**，零 failed。**不再需要 `--ignore`**——孤儿文件 `tests/test_storage_locks.py` 已在本轮经确认后删除（见上文遗留风险表首行） |

## 后续迁移顺序（本期不执行）

1. **写侧切到 `{step}/hls/`**（第 4 期：`recording` 接管、`hls_strategy` 退役）——**这是
   前置**，没切之前域内解码读不到任何生产数据；
2. `Timeline` 删除，`FrameTracker.find` 内部改调 `hls.iter_frames`（`find` 的位级匹配留在
   offline）；`FrameTracker` 的 `track` 参数随之去掉；
3. `SegmentFinder` 的枚举让位给 `hls.list_segments`，VOD 清单构造随之迁入；
4. `sidecar_path` 撤回包内私有——它唯一的域外消费方（`Timeline._load_sidecar`）在第 2 步
   消失。

## 新增的遗留风险

| 风险 | 影响 | 处理计划 |
|------|------|---------|
| **解码有两份实现并存** | 域内 `_decode` 与 offline 的 `Timeline` 同时在世，且读的布局不同（`{step}/hls/` vs 平铺）。改其中一份不会让另一份红 | 与写侧 `hls_strategy` 并存同款自觉状态。第 4 期一并收口；在那之前改动任一份都要想到另一份 |
| **`SegmentRef` 同名不同型** | 域内 2 字段、`segment_finder` 6 字段带 `path`，混用不会立刻报错 | 新代码一律用 `from app.storage import hls` 下的那个。第 3 步迁完后只剩一个 |
| **`_decode` 仍无生产消费方** | 新增能力是死代码，只有单测在跑 | 分期的自觉代价，与第 2 期同一立场。离线视觉特征构造是价值兑现点 |
