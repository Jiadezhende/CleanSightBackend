# 抽出 `step_store`：step 目录的落盘格式独立成 leaf 包，对外只认 `(task_id, step_id)`

> **变更状态**：已生效（2026-09-06 起，2026-09-07 完成接口收口）
> **知识库**：待沉淀
>
> <!-- 本文按最终实现写，不记设计过程。行为变更只有两处，集中在 §7。 -->

## 概述

`database/{task_id}/{step_id}/` 的落盘格式知识此前散在 4 个服务包 + 4 个 router，实测 **12 份重复副本**，并由此产生三条缺陷。现抽出 `app/services/step_store/` 作为**零跨服务依赖的 leaf 包**，写侧读侧共同向它依赖。

对外契约一句话：**调用方只给两个 id，本包回答一切；路径不是对外概念。**

```python
from app.services.step_store import store as step_store

step = step_store.step(task_id, step_id)
step.segments("raw")                                   # 读：段列表（默认已滤在途段）
step.vod_playlist("processed")                         # 读：m3u8 成品，不是骨架
step.product_path("segment", track="raw", ts_us=...)   # 写：该产物落哪
step_store.purge_step(task_id, step_id)                # 删：跨所有写者
```

19 个包外文件向它依赖（4 个 router + 12 个服务模块 + `settings` + `run_control` + `traceback/__init__`），谁也不知道目录长什么样、文件叫什么名。

---

## 变更背景

### 三条实际缺陷

| 编号 | 问题 | 风险 |
|------|------|------|
| P1-① | `purge_step_dir` 的 docstring 列举它「只删 HLS 产物」，实际 `rmtree` 整个 step 目录，连带删掉 `inference` 写的 `features.jsonl` / `facts.jsonl` / `offline_inference_result.json` | 删除者的自述与行为不符 |
| P1-② | `persistence.start_run`（rmtree）必须先于 `FeatureStore.open_fresh`（建文件），否则新文件被抹。约束的**全部载体**是 `run_control.py` 的三行注释，两侧代码互不知情 | 顺序一动即静默丢数据 |
| P1-③ | `cleanup_worker` 只 `glob("*/*/metadata.json")` 判 TTL。有 inference 产物但无 HLS 产物的 step 目录永远扫不到 | `features.jsonl` 确定性泄漏 |

### 12 份重复副本，且全在读侧

VOD m3u8 构造 ×3、EXTINF 解析 ×2（且手法不同）、段文件名正则 ×2、段二分定位 ×2、init.mp4 存在性判据 ×2（代码注释已自认是复制）、ffmpeg 起停 ×4、`finder or SegmentFinder(...)` 构造样板 ×3。

写段本身（tfdt hex-patch / timescale pin / ffmpeg 4.x-8.x cwd 兼容）只有一份，无重复。

### 为什么是布局逼出来的，不是谁偷懒

- **`persistence` 的定位是慢任务异步处理**（队列 + WorkerPool + strategy 分发），`hls_strategy` 只是恰好被异步 worker 调用而寄生其中。
- **`services/traceback/` 是业务语义包**（告警回溯取证），却被 `inference` 和 `lab` 依赖，只为拿一个落盘布局解析器。
- **写侧不敢依赖读侧**：`hls_strategy` 为算 tfdt 偏移要回读自己写的 playlist，但用 `traceback` 那份解析器会造成 `persistence → traceback` 的别扭方向，只好自己再写一遍。

### 触发来源

从「`FrameTracker` 为什么要延伸这么多内部类」这个问题起，逐层追问到公共工具与模块私有的划分，最终定位到 `database/` 读写操作缺一层抽象。

---

## 方案

### 全景：格式知识从「各家自备」变成「共同下游」

```text
改造前                                  改造后
  persistence/hls_strategy  ─┐            persistence/hls_strategy   ─┐
    自建段名正则、EXTINF 解析  │                                        │
  traceback/segment_finder  ─┤            traceback（只剩 MediaToken） │
    段名正则、EXTINF 解析      │            lab/{clip_builder,exporter} ─┼─▶ step_store（leaf）
  lab/clip_builder          ─┼─ 各写各的  inference/{feature,offline}  ─┤     Step 句柄 + 模块函数
  lab/step_exporter         ─┤            routers/*                   ─┘     Step      句柄
  inference/offline         ─┤
  routers/*                 ─┘          step_store 不 import 任何 app.services.*（门禁锁死）
```

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **薄层：布局 + 删除入口 + 产物注册（采用）** | ~1450 行新包，改 19 个包外文件 | 采用。三条缺陷全出在「路径 / 命名 / 删除 / 产物可见性」这一薄公共面上，没有一条出在「内容怎么写」上 |
| 厚层 Repository / DAO | 要搬走批缓冲、目录锁、owner fence、ffmpeg 转码 | 否。这不是「抽一层」，是重写 persistence 与 inference/feature |
| 只去重不建包 | 写侧仍不敢依赖读侧 | 否。重复正是当前布局逼出来的，不动布局就会重新长出来 |

---

## 1. 包结构

```text
app/services/step_store/
  __init__.py          58 行  标记型：docstring + 边界声明。不 re-export，消费方走深路径
  store.py            670 行  **唯一对外面**：Step / SegmentRef / 领域异常 / 5 个模块级函数
  layout.py            92 行  命名真源。纯常量 + 纯函数，L0，任何人零成本 import
  playlist.py         136 行  m3u8 读（EXTINF）与 VOD 骨架写。**包内私有**，两条具名例外
  purge.py            224 行  PRODUCTS 注册表 + 枚举 + 最后活动时间 + 删除动作
  segment_decoder.py  280 行  段 + init → 像素帧。**本包唯一起 ffmpeg 子进程的模块**
```

依赖上界：`layout` / `playlist` / `purge` 是 stdlib only（L0），`segment_decoder` 吃 numpy（L1），`store` 只在函数体内 import `settings`（L3）。**全包重依赖集合为空**（无 torch / cv2 / ultralytics）。

> **`purge.py` 而非 `lifecycle.py`**：后者会被读成它接管了目录生命周期。本包只回答「这目录里有什么、最后何时活动」并执行删除，**不决定该不该删**。

---

## 2. 对外接口

### 2.1 模块级函数（`store.py`）

取句柄与枚举没有身份也没有状态，故是自由函数而非门面类的方法（论证见 §10）。存储根一律自解析，**不收 `base_dir`**。

```python
from app.services.step_store import store as step_store
step = step_store.step(task_id, step_id)
```

**函数一律模块限定，不裸导入**：`step` / `steps` / `tasks` 是调用方的高频局部变量名，裸导入后一句 `steps = steps(task_id)` 就会在函数内遮蔽同名全局，同一函数里再调一次直接 `UnboundLocalError`（`routers/task.py` 就是这个写法）。类型与异常不与局部变量撞名，按名导入。

| 函数 | 签名 | 功能 |
|------|------|------|
| `step` | `(task_id, step_id) -> Step` | 取句柄。**目录不存在也返回** —— 句柄是路由键的载体，不是存在性断言 |
| `steps` | `(task_id=None, include_empty=False) -> List[Step]` | 列 step。`task_id=None` 为全局枚举；`include_empty=True` 含两轨无段的目录 |
| `tasks` | `(recent_first=False) -> List[int]` | 列 task id。`recent_first` 按 step 目录 mtime 粗排，**仅供挑深扫候选** |
| `purge_step` | `(task_id, step_id) -> bool` | 删除整个 step 目录（含所有写者的产物）。只执行，不判断该不该删 |
| `sweep_empty_tasks` | `() -> int` | 回收 step 全被抽走后剩下的空 task 目录 |

`include_empty` 承载的两种语义差是刻意的，用一个显式开关比两个同名不同义的函数安全：默认丢弃「两轨都没段」的 step（起流即失败，不该露给前端点开黑屏），而 **TTL 恰恰要看见这类目录**（P1-③ 泄漏的正是它）。

`purge_step` **刻意不挂 `Step`**：它跨所有写者、没有单一归属；且作为自由函数意味着调用方必须重新写出两个 id，不能顺手 `step.purge()`。名字带 `_step` 后缀是硬约束 —— `store.py` 顶部已 `from ... import purge` 绑了 `purge` 这个名字，同名函数会把模块遮掉。

### 2.2 `Step`（句柄）

路由键绑一次。目录扫描（单次 `iterdir` 出双轨段）与逐轨 playlist 解析首次访问时做一遍，之后在**本对象生命周期内**缓存。

⚠ **不得跨请求持有**：活跃 step 每 ~10s 落新段，缓存会过期。

| 成员 | 签名 | 功能 | 消费方 |
|------|------|------|--------|
| `task_id` / `step_id` | `int` | 路由键 | — |
| `tracks` | `Tuple[str, ...]` | 实际落盘的轨 | `routers/{lab,task}` |
| `time_bounds_us` | `(int, int) \| None` | 双轨并集起止，微秒。**终点含末段 EXTINF** | `routers/traceback`（timeline） |
| `last_activity_at` | `float \| None` | 最后一次产出时刻（unix 秒），取已登记产物的 mtime 最大值 | `cleanup_worker` |
| `segments` | `(track, playable_only=True) -> List[SegmentRef]` | 该轨全部段，ts 升序。**默认滤在途段** | 全部读侧 |
| `segments_around` | `(ts_ms, track, before=1, after=2) -> (List[SegmentRef], int)` | 包含 `ts_ms` 的段 + 前后上下文，及触发段**在返回列表内**的下标 | `routers/traceback`（告警取证） |
| `vod_playlist` | `(track, segments=None, encode_uri=None) -> str` | VOD m3u8 **成品**（含 `ENDLIST`） | `routers/traceback` ×2、`lab/step_exporter` |
| `has_init` | `(track) -> bool` | 该轨 fMP4 init 段是否就位 | `lab/clip_builder` |
| `find_product` | `(filename) -> Path \| None` | 按**外部给的**文件名取产物；含 path traversal 校验 | `routers/media` |
| `frames` | `(track, start_ts, end_ts, width, height) -> Iterator[Frame]` | 区间解码为像素帧。**起 ffmpeg 子进程** | `inference/offline/frame_finder` |
| `product_path` | `(kind, **key) -> Path` | 该产物的写位置，顺带保证 step 目录存在 | `hls_strategy`、`feature/store` |
| `open_product` | `(kind, mode="r", *, encoding=None, **key) -> IO` | 直接开文件句柄，路径不出包 | `feature/store`、`offline/runner` |
| `scratch_path` | `(prefix, suffix=".m3u8") -> Path` | step 目录内的临时文件路径（前导点命名，非产物） | `lab/*`、`feature/store` |

四条关键约束（写错会**静默**出错，故都收进包内）：

- **`segments(playable_only=True)` 是默认值**。在途段 = mp4 已落盘（扫得到）但 transcode+append 未完成（不在 playlist 里）。放它们过去会让 fragment 实际媒体时长与 playlist 声明对不上：回放侧是 hls.js MSE 缓冲洞，导出侧是时长错乱。清单类接口（大屏历史、lab 任务列表、clip_builder、解码）传 `False` —— 它们只报「磁盘上有没有画面」，不解码也不拼 m3u8。
- **`segments_around` 刻意不滤在途段**：取证要的是「那一刻的画面在哪个文件里」，滤掉会让刚落盘的告警取不到证据。拼 m3u8 时 `vod_playlist` 会再滤一道。
- **`time_bounds_us` 的终点取 `max(seg.ts + EXTINF)` 而非 `max(seg.ts)`**，后者会漏掉最后一段自身长度。EXTINF 是 hls.js / fragment 媒体时长的同源真值，对齐到它前端时长才与 `<video>.duration` 一致。
- **`open_product` 写模式建目录、读模式不建**。读一个不存在的 step 不该在盘上留下空目录 —— 空目录无任何已登记产物，`last_activity_at` 返回 `None`，TTL 按契约不删它，等于永久泄漏。

`vod_playlist` 的 `encode_uri(kind, filename)` 默认恒等（裸文件名就是合法相对 URI）。本包只写**段的身份**，token 化 URL 是访问控制、属协议层，从这个口子注入。缺 init / 无可播段抛 `StepInitMissing` / `StepNoPlayableSegments`，映射成什么状态码由调用方决定。

### 2.3 `SegmentRef`（值对象）

`NamedTuple(filename: str, ts_us: int)` + 派生只读 `ts_ms` / `ts_s`。刻意不带的五样：

| 不带 | 理由 |
|------|------|
| `task_id` / `step_id` | 调用方持句柄就有。曾经带过，结果是 `routers/traceback.py` 相邻两行一个从 ref 取、一个从请求路径取，同一个值绕了一圈 |
| `track` | 零消费方（包内 `SegmentDecoder` 用构造时传入的那个） |
| `path` | 两个消费方实际都只用 `.name`，即 `filename` |
| `is_trigger` | 那是**查询上下文**不是段属性，改由 `segments_around` 出下标 —— 顺带消掉「为设一个 bool 重建整批 frozen 副本」的 10 行样板 |
| `duration` | `vod_playlist` 收口后包外无消费方；EXTINF 与 `filename → 时长` 映射一并留在包内 |

判据两条，方向相反但同一把尺子 —— 看调用点有没有在做本可以不做的事：**调用方已持有的信息，值对象不重复携带**（重复携带让调用点去选「用哪个真源」）；**调用方总要 join 的信息，值对象一次带全**。

### 2.4 `PRODUCTS` 产物注册表（写侧真源）

9 类产物，每项记 `kind` / `pattern` / `name` 构造函数 / `writer`：

| kind | 文件 | 写者 |
|------|------|------|
| `segment` / `init` / `playlist` / `sidecar` / `metadata` | `*_segment_*.mp4` / `*_init.mp4` / `*_playlist.m3u8` / `*_segment_*.idx` / `metadata.json` | `persistence/hls_strategy` |
| `features` / `facts` | `features.jsonl` / `facts.jsonl` | `inference/feature/store` |
| `offline_result` | `offline_inference_result.json` | `inference/offline/runner` |
| `visual_roi`（三期，未落地） | `visual_roi_*.npz` | `inference/offline/visual` |

**新增落盘产物必须在此登记**，否则 `product_path` 的 kind 校验抛 `KeyError`。此前忘登记不报错，只表现为「目录被提前回收，而那个产物还有人要读」。

`writer` 字段**只作文档与排查用途，不是权限** —— 写成员的调用者由导入门禁按模块限制（见 §6）。

> **一个由测试抓出来的坑**：`hls_strategy` 转码期的临时文件 `.{stem}.tmp_init.mp4` 会命中 `*_init.mp4`、`.{stem}.tmp_seg_0.mp4` 会命中 `*_segment_*.mp4`，光靠 glob 区分不开。若被算作产物，崩溃残留会让死 step 永远显得活跃、躲过回收。现按**前导点**统一排除（step 目录里所有临时文件都以 `.` 开头），`scratch_path` 保证这条约定。

### 2.5 不提供的能力

| 想要 | 去哪 |
|------|------|
| step 目录路径 / 存储根 | **无**。要 ffmpeg `cwd` 取 `scratch_path().parent` 或已定位产物路径的 `.parent` |
| 文件命名函数（段名 / init / playlist / sidecar / metadata） | 包内私有，走 `product_path(kind)` |
| 段级二分原语 `_locate_containing_index` | 包内私有（两个消费方都在包内，off-by-one 论证只需写一处） |
| VOD m3u8 的骨架与语法 | 包内私有。**对外只出成品** |
| `filename → EXTINF` 映射、在途段过滤 | 包内私有；已被 `segments(playable_only=True)` 与 `vod_playlist` 吸收 |
| 已登记产物清单 | 不提供；TTL 只要 `last_activity_at` |
| 写未登记的产物 | 无。先进 `PRODUCTS` |
| 目录锁 / 并发控制 | 各写者自持（见 §5） |
| 「该不该删」的保留策略 | `persistence/workers/cleanup_worker` |
| ffmpeg 转码参数、tfdt patch、cv2 写帧 | `persistence/strategies/hls_strategy` |
| 按 ts 精确对号取帧 | `inference/offline/frame_finder` |
| lab 导出根 / lab 配置文件位置 | `settings.lab_export_root` / `settings.lab_runtime_config_path` |
| HTTP 状态码、token 化 URL、`MediaToken` | `routers` / `services/traceback` |

> **「不出路径」≠「不出 `Path`」**。ffmpeg 必须吃真路径（`cwd` + basename 是 4.x/8.x 唯一兼容写法），硬包起来就是无知识可承载的包装。**禁的是调用方自己拼**，不是禁 `Path` 出现在返回值里 —— 故本包不提供任何返回**目录**的成员：目录 = 根 + 两级 id 的拼装公式，交出去等于把布局复制一份到调用方，**且门禁抓不到**（`dir / "x"` 是普通 Path 拼接，不是 settings 访问）。

---

## 3. 包内的格式知识（去重结果）

每一样都带一段论证、写错会**静默**出错 —— 这正是「值不值得抽」的判据：重复值不值得抽，看它承载的知识显不显然。

| 知识 | 前 | 后 | 写错的表现 |
|------|----|----|-----------|
| 段文件名正则 | 2 份（写侧位置 group / 读侧 named group） | `layout.SEGMENT_PATTERN` | — |
| sidecar 命名 | 写侧 f-string 拼、读侧 `with_suffix` 反推，靠「stem 长一样」这个隐式契约对上 | `sidecar_name` / `sidecar_name_for` 同模块内互为逆运算 | **静默丢帧**（读侧按契约只 warning 跳过该段，那个宽容本身是对的） |
| `ts_to_us` 截断而非四舍五入 | 隐式约定 | 写进 `layout.ts_to_us` docstring | 「start_ts 恰为该段首帧」的定位**无条件出错** |
| EXTINF 解析 | 2 份，正则 vs 字符串切，容忍度靠巧合一致 | `playlist.parse_playlist_entries`（保序）+ 两个投影 | 见下 |
| 段二分定位 | 2 份，`bisect_right-1` vs `np.searchsorted(...,'right')-1` | `_locate_containing_index`，返回 -1 交调用方处理 | 用 `bisect_left` 是**无条件错**（既取到 target 之后的段，又因 ts_us 是截断值而漏掉「target 恰为段首帧」） |
| init 判据 / 在途段过滤 | 各 2 份（注释已自认是复制） | `Step.has_init` / `segments(playable_only=)` | hls.js 段尾停摆、缓冲洞 |
| VOD m3u8 骨架 | 3 份，8 行头部逐行重复 | `playlist.build_vod_playlist`（包内）+ `Step.vod_playlist`（对外成品） | 缺 `ENDLIST` → 播放端当直播流只读 live edge，前面的段全丢 |

**EXTINF 是段时长的唯一真值，不能用文件名 ts 差重推** —— 那是 wall-clock 抖动值，与 fMP4 fragment 的实际媒体时长不一致。写侧的 tfdt 起点同源：`tfdt(N) = Σ EXTINF(0..N-1)`。

> **统一 EXTINF 解析带来一处行为差异（更严格，非回归）**：旧正则对**孤立** `#EXTINF:` 行（后面没跟文件名，只可能出现在 append 写到一半崩溃时）也计入求和；新解析器要求 EXTINF 与文件名成对才计入。孤立条目意味着那个段并未真正进入 playlist，**不计入才是对的** —— 旧算法会让该段之后所有 fragment 的 tfdt 偏移偏大。

### sidecar 的名字与内容必须同居本包

`.idx` 这一个文件，抽包前归三个包管：

```text
  写内容   hls_strategy._update_timeline        persistence
  命名     layout.sidecar_name / _name_for      step_store
  读内容   Timeline._load_sidecar               inference/offline
           + 段内帧号 n ↔ 下标 k 的 1:1 对齐
```

那个 1:1 对齐是整个索引的地基（靠不拼 m3u8、不用 `-ss`、`-vsync 0` 保证，破了不报错、只静默取错帧）。写侧读侧隔着两个包，改一侧看不见另一侧。现在名字与内容同居 `step_store`，与写侧只隔一层。

---

## 4. 各消费方改了什么

### 4.1 读侧

| 文件 | 改动 |
|------|------|
| `routers/traceback.py` | m3u8 构造 **102 → 12 行**：只剩 token 签发（经 `encode_uri` 注入）与领域异常 → HTTP 状态码。`_step_duration_ms` 用 `time_bounds_us`；`is_trigger` 由 `segments_around` 的下标算出，**响应 JSON 形状不变** |
| `routers/media.py` | path traversal 防御下沉到 `Step.find_product`，本层只把「拿不到」映射成 404，不碰存储根、不拼目录 |
| `routers/task.py` | `/history` 的三个时间字段由 `segments(track, playable_only=False)` 的 ts 推，**口径与响应逐字节不变**（见 §7.3） |
| `routers/lab.py` | `_raw_steps` 出**句柄**而非 id —— 调用方拿到后还要读段时间戳，句柄里的扫描结果已缓存，再问一次不重扫盘 |
| `lab/step_exporter.py` | 删掉自己的 `_build_vod_text`，改 `Step.vod_playlist`；捕获领域异常翻成自家 `StepExportInitMissing` / `StepExportNoSegments`（路由的异常契约不变） |
| `lab/clip_builder.py` | 句柄绑一次往下传：`_select_segments` 与 `_validate_continuity` 都要全量段，此前各扫一遍盘 |
| `inference/offline/frame_finder.py` | 默认解码源改 `Step.frames`；`decoder=` 注入口保留（测试 seam） |

**每请求扫盘次数**：traceback playlist 此前段列表 / EXTINF / init 判据各扫一遍，现在句柄内缓存 → **1 次 `iterdir` + 每轨 1 次 playlist 解析**。

### 4.2 写侧

| 文件 | 改动 |
|------|------|
| `persistence/strategies/hls_strategy.py` | 全部 `target_dir / layout.*` → `step.product_path(kind, ...)`；ffmpeg `cwd` 取 `product_path(...).parent`；**丢掉 `db_dir` 构造参数**；`_dir_locks` 的 key 由路径字符串换成 `(task_id, step_id)` 元组 |
| `inference/feature/store.py` | `_JsonlBuffer(kind)` 取代 `(base_dir, suffix)`，落盘位置改问 `step_store.step(...)`；两个 `load()` 走读模式 `open_product` + catch `FileNotFoundError`。**`_lock` / owner fence / 批缓冲一律不动** |
| `inference/offline/runner.py` | 丢掉 `base_dir`（§10 后连 `store=` 也不收，一律自解析）；`offline_inference_result.json` 走 `open_product` |

`release_dir_locks` 此前要拼一遍 `db_dir/task_id/` 前缀才能匹配锁 key，等于让**并发机制依赖目录布局**；换成元组后按 `k[0] == task_id` 剔除。

### 4.3 存储根的传递链整条拆掉

```text
拆前  settings.storage_base_dir
        ├─ PersistenceConfig.storage_base_dir  （它本就是 `return settings.storage_base_dir`）
        │    └─ HLSWorkerPool(db_dir=) ─▶ HLSPersistenceStrategy(db_dir)
        ├─ InferenceManager._db_dir ─▶ FeatureStore(base_dir)
        ├─ OfflineRunner(base_dir=) / offline/cli 直读
        └─ SegmentFinder.base_dir ─▶ lab 导出根 ×2、lab 配置文件

拆后  settings.storage_base_dir                          只被 step_store 读（门禁锁死）
      settings.lab_export_root / lab_runtime_config_path 存储根**旁边**的东西，不向 step_store 借根
```

> 拆到这一步还剩一截：根虽然只有 step_store 读得到，但 `StepStore(base_dir)` 这个构造参数仍在，7 个消费类各留了一个 `store: Optional[StepStore]` 形参替测试转运它。§10 把这一截也拆了。

---

## 5. 刻意不动的三样

### 并发：锁不进本包

现有两把锁各在自己的 writer 手里，维持现状：

| 锁 | key | 保护的不变式 |
|----|-----|-------------|
| `hls_strategy._dir_locks` | `(task_id, step_id)` | tfdt 累计偏移不能碰撞（相邻段 transcode 读到相同累计 EXTINF 即撞） |
| `FeatureStore._lock` | 实例 | 缓冲区状态 + owner fence |

第一把的 key 恰好就是 `Step` 的路由键，看起来该收进来。**不收，三条理由**：

1. **本包出的是随手构造的无状态句柄，锁表必须进程级单例** —— 挂到句柄上等于每次拿到不同的锁、完全失效；挂到模块上则是给共享层塞进程级状态。
2. **锁保护的不变式是 writer 私有的**。本包能提供「按 step 串行」这个机制，但不知道**为什么**要串（tfdt 累计偏移是 HLS 写侧知识）。机制搬走、语义留下，与 sidecar「命名在这边、内容在那边」是同一类错误。
3. **进程内锁给不出跨进程保证**（offline runner 是独立进程）。放进共享层等于对所有写者承诺「写是安全的」，而该承诺跨进程不成立 —— **虚假安全感比没有更糟**。

冲突面盘点：HLS 多 worker 写同一 step（目录锁，够）；FeatureStore 多线程（实例锁 + owner fence，够）；跨写者（**不需要** —— 三者写不同文件，`PRODUCTS` 的 kind 划分保证互不重叠）；`purge` vs 写（**真缺口**，见 §8）。

### 写权限：不做类型级隔离

一度想拆 `StepWriter`、由门面发放。**推翻了** —— 枚举公开、句柄可随手构造，谁都能自己发一个。那不是能力对象，是**命名仪式**：多一个类型，零强制力。

更根本的是问题找错了。回看三条缺陷：P1-① 是文档问题，P1-② 是调用序问题（锁和权限都解不了），P1-③ 是**产物没登记**。**没有一条是「谁越权写了不该写的东西」** —— 三个写者各写各的文件，从未互相踩过。

故最终形态：

| 手段 | 防什么 | 强制力 |
|------|--------|--------|
| `kind` 必须在 `PRODUCTS` 里 | 新增产物忘登记 → TTL 不可见 → 目录被提前回收而产物还有人读 | 运行时抛，真强制 |
| 导入门禁按模块限制写成员的调用者 | 计划外的第四个写者混进来 | AST 静态检查，真强制 |
| ~~`Writer` 枚举 + `writer_for`~~ | ~~越权写~~ | **零** |

> **判据**：类型不产生强制力时，多一个类型只是把约定写得更长。Python 里真正拦得住的是运行时校验与静态门禁 —— **要拦就拦在这两处；拦不住的，别用类型假装拦住了。**

### ffmpeg 起停不收口

四处 ffmpeg 调用的 bin 解析 / 超时预算 / stderr 格式化看着像重复，**评审后不抽**：`resolve_bin()` 是 3 行、`budget(floor,n,per)` 只是把 `max(floor, n*per)` 重新命名一遍、`format_failure` 的 4 处输入形态本就不同（PIPE 的 str vs 临时文件的 fd）。它们没有可论证的知识，重复三遍不产生风险，抽出来也不消除风险。

同理，三处 stderr 截断长度（1500 / 500 / 全量）的不一致**不是缺陷**：面对的读者不同（异常给调用方、warning 给运维），不一致合理，不等于重复。

---

## 6. 门禁（`tests/test_import_hygiene.py`，12 个用例）

| 门禁 | 拦什么 |
|------|--------|
| `test_step_store_is_a_leaf` | 本包 import 任何 `app.services.*` / `app.routers`。破这条即 leaf 地位失守，写侧读侧的循环依赖会重新长出来 |
| `test_storage_root_is_private_to_step_store` | 除 `app/settings.py` 与本包外访问 `settings.storage_base_dir` |
| `test_step_store_write_members_have_registered_callers` | `product_path` / `open_product` 被 `PRODUCTS` 登记之外的模块调用 |
| `test_step_store_playlist_is_package_private` | m3u8 骨架出包 |
| `test_import_budget["app.services.step_store"]` | 重依赖集合非空 / 导入耗时超 1.0s |

**存储根那条的判据是「根拿不到，路径就无从拼起」** —— 这比「数还有几处 `/` 拼接」可查得多，因为 `root / "x"` 是普通 Path 拼接、门禁看不出它在拼 step 目录。拦在源头才拦得住。

`playlist` 模块的两条**具名例外**，各写明退出条件：

| 例外 | 理由 | 退出条件 |
|------|------|---------|
| `persistence/strategies/hls_strategy` | 它是 playlist 格式的**定义者**（手写 EXTINF 行与文件头、`sum_durations` 算 tfdt 前缀和），不是消费者 | 永久 |
| `lab/clip_builder` | 每段 EXTINF 取相邻段 ts 跨度而非 playlist EXTINF —— seek 基准本就是 ts，换了会逐段错位 | 验证两者在 fps 漂移下等价后改走 `Step.vod_playlist`，并删掉门禁里那一行 |

---

## 7. 行为变更（只有两处）

### 7.1 TTL 判据换代：`metadata.json.updated_at` → 已登记产物 mtime 最大值（修 P1-③）

| 候选判据 | 为什么不用 |
|---------|-----------|
| `metadata.json.updated_at`（旧） | 那是 **HLS 独有**产物。只有 `features.jsonl` 而无 HLS 段的 step（HLS 未启用，或首段 transcode 失败但推理照常跑）永远扫不到 → 无限期堆积 |
| 目录 mtime | 会被临时文件的增删刷新，崩溃残留的 `.export_*.m3u8` 能让死 step 永远显得活跃 |
| **已登记产物 mtime 最大值（采用）** | 只认真产物；活跃 step 每 ~10s 落新段，mtime 随之更新，不会误删 |

枚举也从 `glob("*/*/metadata.json")` 改为 `steps(include_empty=True)`。

`cleanup_worker` 改造后的循环：枚举 → 要 `last_activity_at` → **自己**比 cutoff → 调 `store.purge`。它仍然完整拥有「删不删」的决定权，只是不再自己发明「怎么判断这目录还活着」。两侧 docstring 互指。

新增 `StorageCleanupWorker(dry_run=True)`：只统计与打印、不真删。**不进配置文件**（判据换代时的一次性验证工具，不值得扩配置面），用临时脚本传参。

### 7.2 `EXT-X-TARGETDURATION` 三种算法统一为 `round`

`vod_playlist` 收口后，`traceback`（`round`）/ `step_exporter`（`ceil`）/ `clip_builder`（`int+1`，未迁移）的三份备料合并，取 `round`。

**这解决了改造中途提出的「`round` 疑似违反 RFC 8216」，且结论与当时的猜测相反**：RFC 8216 §4.3.2.1 的判据是「EXTINF **四舍五入后** MUST ≤ TARGETDURATION」，故 `round` 才是正解，`ceil` 只是恰好也安全。

影响面：`step_exporter` 喂给本地 ffmpeg 的临时 m3u8 文本变化，**非对外产物**。

### 7.3 不在此列：`/task/history` 的时间口径

改造中一度计划把 `/history` 的 step 时间字段换成 EXTINF 口径（末段时长计入）。**主动没做**：那会把 `last_segment_ms` 从「末段起点」变成「末段终点」，属对外契约变更；且 `time_bounds_us` 无 playlist 时返回 `None`，会让历史遗留 / 首段仍在 transcode 的 step 直接从清单消失。

现改由 `segments(track, playable_only=False)` 的 ts 推导，**响应逐字节不变**，`docs/api/task.md` 不动。EXTINF 口径留在 `/timeline`（它本来就是精确时长的出口）。

---

## 8. 修 P1-① 与 P1-②

- **P1-①（文档与行为不符）**：`purge.purge_step` 与 `hls_strategy.purge_step_dir` 的 docstring 都如实声明删的是**整个目录含所有服务的产物**。目录锁留在 `hls_strategy` —— **锁是并发机制，不随删除动作搬家**。
- **P1-②（rmtree vs open_fresh 顺序）**：约束从「只活在 `run_control` 的三行注释里」提升为**三方 docstring 互指**（`purge.purge_step` / `FeatureStore.open_fresh` / `run_control.start_run`）。

运行时断言不做 —— 那要引入「当前 run 起始时刻」的状态，超出 `purge` 的无状态定位。**这个缺口用锁也解不了**：调用方（cleanup_worker）与写方可能在不同进程，`threading.Lock` 无效；跨进程要文件锁，Windows / Linux 两套 API，且锁文件本身得加进前导点排除表。实际窗口只有 run 起始与 TTL 回收两个时刻，成本高于收益。

---

## 9. 连带的正名

| 前 | 后 | 理由 |
|----|----|----|
| `traceback/segment_finder.SegmentFinder` | `step_store/store`（模块，§10 后不再是类） | 原类已承载 purge、产物、临时文件、traversal 防御，名字与职责脱节 |
| `Timeline` | `SegmentDecoder` | 名字像数据结构，实际是解码器 |
| `FrameTracker` | `FrameFinder` | `Tracker` 在 CV 语境专指目标跟踪，且与三期 `SlotTracks` 撞词。与 `segments_around` 成对仗：**段级找段、帧级找帧** |
| `get_default_base_dir` | `storage_root` | `default` 暗示它是「没传时的兜底值」，但它是唯一真源 |

`SegmentDecoder` / `FrameFinder` 分居两包，理由是**失败契约相反**：前者区间扫描、**宽容**（缺 sidecar 跳过该段、空区间返回空），后者点查、**严格**（任一 ts 配不上就 `ValueError`）。宽容留在格式层，严格留在消费侧策略。

`SegmentDecoder.for_step(step, track)` 类方法：**「解码要 `Step` 的哪几样」只写这一处**，`Step.frames()` 与测试的 seam 子类走同一条构造路径，不会分叉。

命名规则（写进 `Step` docstring，防后人加成员时打架）：

| # | 规则 | 例 |
|---|------|-----|
| 1 | **代价用 property / method 区分，不用词性**。property = 零成本或随目录扫描已缓存；method = 至少一次 I/O | `tracks` / `time_bounds_us` ↔ `segments()` / `frames()` |
| 2 | **动词只留给变盘的成员**。查询无论多贵都用名词 | `purge()` / `open_product()` ↔ `frames()`（起 ffmpeg 但不变盘，仍是名词 —— 成本写在 `Iterator` 这个类型里） |
| 3 | 两个惯例前缀不受规则 2 管：`has_`/`is_` 表布尔，`find_` 表「可能找不到」 | `has_init()` / `find_product()` |
| 4 | **出 `Path` 的以 `_path` 结尾**。本包不出目录，故没有 `dir` 结尾的成员 | `product_path` / `scratch_path` |
| 5 | 同一资源的读写共用词根，前缀区分输入可信度 | `product_path`（按登记 kind 算，必返回）/ `find_product`（按外部文件名找，可能 `None`）/ `open_product` |
| 6 | 带单位或时基的量写进名字 | `ts_us` / `time_bounds_us` / `last_activity_at` |
| 7 | 单复数对仗表单个 / 批量 | `step()` ↔ `steps()`；`segments` / `segments_around` |

---

## 10. 门面降为函数：`StepStore` 删除，存储根注入链整条拆掉

接口收口后回看 `store.py`，两个类型的成色差得很远：

| | 有没有状态 | 有没有身份 | 结论 |
|---|---|---|---|
| `Step` | 有（`_by_track` / `_durations` 两级缓存） | 有（`(task_id, step_id)`，实现了 `__eq__` / `__hash__`） | **该是类** |
| `StepStore` | 一个字段 `_base_dir` | 无 | **不该是类** |

而那唯一的字段，**生产代码从来不传**（`instance.py` / `offline/cli.py` / 各 router 全走默认自解析）。它只服务测试指临时根这一件事，代价却是在 7 个消费类里各长出一个 `store: Optional[StepStore]` 构造参数：`HLSPersistenceStrategy`、`_JsonlBuffer` / `FeatureStore` / `FactLedger`、`InferenceManager`、`OfflineRunner`、`ClipBuilder`、`StepExporter`。一层没有状态的薄壳，把一个只为测试存在的参数扩散进了半个服务层 —— §4.3 拆掉的那条传递链，其实还剩这一截换了个形态活着。

**改法**：`StepStore` 的五个方法原样降为 `store.py` 的模块级函数（`purge` → `purge_step`，避让同名的包内 `purge` 模块），`Step` 自持存储根、首次碰盘时才解析并缓存 —— **惰性是契约的一部分**：`steps()` 一次可构造上百个句柄，构造期解析等于每个句柄都走一遍 settings + `Path.resolve()`，破坏「构造零成本、不碰盘」。7 个消费类的 `store=` 形参全部删除。

**存储根只剩一条路**：`settings.storage_dir`。测试指临时根统一走 conftest 已有的 `tmp_storage` fixture（monkeypatch 该字段），此前 `test_step_store_purge` / `test_persistence_cleanup_worker` / 两个 router 测试就是这么写的，本次把余下 8 个测试文件、约 45 处 `StepStore(tmp_path)` 一并收编（含 4 处手工 `tempfile.mkdtemp()`，顺带拿到自动清理）。

**评估过并否决的备选**：在模块函数与 `Step` 上保留可选 `base_dir`（仅测试用）。7 个消费类的 `store=` 既然要删，它们的测试只能走 `tmp_storage`，保留 `base_dir` 换不回便利，只会让测试套里同时存在两套指根机制。

**已知代价**（未变好也未变坏）：用例漏加 `tmp_storage` 会静默写到真实 `./database` 并读到脏数据，不报错。这个坑在 `StepStore()` 无参回落 settings 时就存在，两个方案都堵不住；真要堵需另加一个 autouse 守卫 fixture，本次未做。

## 11. `store.py` docstring 瘦身：删掉与本文重复的设计辩护

抽包时把大量「为什么这么设计」写进了 `store.py` 的 docstring，而同一批论证本文 §2.3 / §5 / §9 / §10 已有一份 —— 代码里那份是**副本不是真源**。670 行的文件里 292 行是 docstring、仅 211 行是代码。现按下述规则裁到 230 行 docstring（-21%），**代码零改动**（去 docstring 后 AST 与改前逐节点相等，已脚本校验）。

| 处置 | 内容 | 去处 |
|------|------|------|
| **留** | 签名语义、单位、`Args` / `Returns` / `Raises` | 原地 |
| **留** | 写错会**静默出错**的陷阱：`bisect_right` 不能换 `left`、EXTINF 不能用文件名 ts 差重推、`playable_only` 默认值的后果、临时文件前导点命名、`open_product` 读模式不建目录、`time_bounds_us` 终点须含 EXTINF | 原地（删了就真的没人知道了） |
| **删** | 「曾经带过 X 字段，结果是…」「这段检查曾写在 `routers/media.py`」「此前 `PRODUCTS` 只是文档性注册表」等考古 | 本文 §2.3 / §9 / §10 已有 |
| **删** | 「刻意不挂在 `Step` 上」「为什么降为函数」等设计辩护 | 同上，代码里压成一句结论 + 锚点 |
| **删** | 模块 docstring 里与 `__init__.py` 重复的包级契约（原 32 行有 ~90% 是副本） | `__init__.py` 是包契约真源 |

判据是**唯一真源在哪**：调用方读签名时必须知道的留在代码里，回答「当初为什么」的留在本文。

自测：`pytest tests/ -k "step_store or import_hygiene"` **88 passed**；全仓无 `__doc__` / doctest 消费方。

## 12. `purge.py` 名实不符：拆出 `products.py`

原 `purge.py` 模块名是一个**动作**，装的却是一份**注册表**：7 个对外成员里只有 2 个是删除。

```
Product / PRODUCTS / product_name    命名与注册表   ← 重心在这
iter_steps                            枚举
list_products / last_activity         目录内容查询
purge_step / sweep_empty_tasks        删除          ← 只有这两个对得上模块名
```

后果是**写一条段的正常路径要穿过一个叫「清除」的模块**：`Step.product_path("segment", …)` → `purge.product_name` → `layout.segment_name`。在写侧读到 `purge` 会以为走错分支。

按内聚主题重切，`purge.py` **整个删除**：

| 去处 | 内容 | 依赖 |
|------|------|------|
| `products.py`（新，171 行） | `Product` / `PRODUCTS` / `product_name` / `_products_in` / `list_products` / `last_activity` / `iter_steps` —— 「这目录里有什么、最后何时活动」 | stdlib + `layout` |
| `store.purge_step` / `store.sweep_empty_tasks` 的函数体 | `rmtree` 整个 step 目录 / `rmdir` 空 task 目录 | `shutil`（store.py 新增 import） |

先拆成 `products.py` + 瘦身版 `purge.py`（72 行），随后判定**删除动作不值得单独成模块**：它不看产物清单、也没有 `store` 以外的调用方，留一个文件只是为了让门面写 `return purge.purge_step(...)` 这行纯转发 —— 与 §10 把 `StepStore` 类降为函数是同一条理由。故 `purge.py` 删除，两个函数体直接写进门面。

`store.py` 因此 608 → 641 行（+33，含合并进来的 ⚠ 契约条款）。

**包外零影响**：改前 `purge` 模块全仓唯一的代码 import 在 `store.py`（其余出现均在注释/docstring 里）。同步改的引用：`cleanup_worker.py` / `inference/feature/store.py` / `run_control.py` 的互指 docstring、`__init__.py` 模块清单、`test_import_hygiene.py` 的两处注释、`test_step_store_purge.py` 的 import 与 5 处调用（该文件名保留 —— 它测的行为还在，只是不再对应一个模块）。

### 未处理：「这文件叫什么」仍有两个真源

HLS 那 5 类名字在 `layout` 的具名函数里，推理侧 4 类（`features.jsonl` / `facts.jsonl` / `offline_inference_result.json` / `visual_roi_*.npz`）以 lambda 形态内联在 `products.PRODUCTS` 里。分界线是「谁写的」而非「什么种类」，属历史遗留。**本轮决定不动**，只在 `__init__.py` 与 `products.py` 的 docstring 里写清分界并注明「新增产物按此归属登记，别再扩大分裂面」。

自测：全量 `pytest tests/` **519 passed**（与 §「自测结果」基线持平，零用例改断言）。

---

## 13. 全包 docstring 复核：口径对齐 + 去重（§11 的收尾）

§11 只瘦身了 `store.py`，其余 5 个文件的 docstring 是抽包过程中陆续写的，存在三类问题。本轮逐份重写，**代码零改动**（AST 逐节点对照，6 份文件全部 SAME）。

**修掉的与事实不符处**（这是本轮的主要价值，行数只是副产物）：

| 处 | 原文 | 实际 |
|----|------|------|
| `__init__.py` | 「12 份重复副本」 | 与提交信息「消 8 份」不一致，且随几轮重构已无从核对 → 删掉具体数字，只留结论 |
| `__init__.py` / `store.py` / `layout.py` | 路径模板写 `{base_dir}/...` | 字段名是 `settings.storage_base_dir`，包内一律称「存储根」/`storage_root()` → 统一为 `{storage_root}` |
| `store.scratch_path` | 「`purge` 靠它把临时文件排除在产物之外」 | `purge.py` 已在 §12 删除，该逻辑在 `products._products_in` → 改指 `products` |
| `layout.ts_to_us` | 「见 finder 的段级二分」 | §9 已把 `FrameTracker` 正名为 `FrameFinder`，且段级二分在 `store._locate_containing_index`（finder 是帧级） → 改指正确位置 |
| `segment_decoder.iter` | 末尾两句重复申明「不做 ts 匹配校验」 | 合进首行 |

**去掉的重复**：同一条论证此前在多处各写一遍，本轮只留真源 + 指针。

- 「路径/存储根不出包」：`__init__.py`（包契约真源）留全文，`store.py` 模块 docstring 压成一句 + 指针。
- 「只出 m3u8 成品不出骨架」：`playlist.py` 留全文，`Step.vod_playlist` 不再复述。
- playlist 两条具名例外的**理由与退出条件**：真源是 `test_import_hygiene.py` 的门禁注释（改名单必须改那里），docstring 压成一行点名 + 指针。
- `layout` / `products` / `segment_decoder` 模块 docstring 里的抽包考古（「此前读侧用 named group、写侧用位置 group」等）：本文已有，代码里删。

**保留不动**：写错会**静默出错**的陷阱，判据同 §11 —— `bisect_right` 不能换 `left`、EXTINF 不能用文件名 ts 差重推、`playable_only` 默认值的后果、sidecar 命名两个方向必须同改、临时文件前导点、`open_product` 读模式不建目录、`time_bounds_us` 终点须含 EXTINF、`purge_step` 与产物创建的先后序、「段内帧号 n ↔ sidecar 下标 k 严格 1:1」的三条保证。这些删了就真的没人知道了。

| 文件 | 行数 | docstring 行数 |
|------|------|---------------|
| `__init__.py` | 68 → 55 | 68 → 55 |
| `layout.py` | 103 → 98 | 51 → 47 |
| `playlist.py` | 136 → 122 | 68 → 54 |
| `products.py` | 171 → 163 | 71 → 63 |
| `segment_decoder.py` | 280 → 271 | 59 → 50 |
| `store.py` | 641 → 604 | 241 → 204 |
| **合计** | **1399 → 1313**（-6%） | **558 → 473**（-15%） |

减幅不大是预期内的：§11 已经砍过一轮设计辩护，剩下的多数是签名语义与静默陷阱，按判据不该删。本轮的产出是**口径正确**，不是行数。

自测：AST 对照 6 / 6 SAME（去 docstring 后逐节点相等）；全量 `pytest tests/` **519 passed**。

## 14. 顺带清掉的两笔：`app/services/__init__.py` 的转发、`settings.py` 的注释

**（a）删掉 `app.services` 的 re-export**（原「遗留风险」表里记为「另案」的那条，本轮做掉）。

原文一句 `from .client import client_manager` + `except ImportError: client_manager = None`，**零消费方**（删除前全仓引 `client_manager` 的位置一律走 `from app.services.client import ...` 深路径），却让每个 `app.services.*` 子模块的 import 都付过路费。两笔代价：

- **耗时**：空跑 `import app.services` 0.303s → **0.002s**；`app.services.step_store` 0.3s+ → 与空跑同量级（热 `__pycache__` 下 ~0.001s），且不再被顺带把 `settings` 拉进 `sys.modules`。
- **静默换错**：那个 `except` 会把真实的 ImportError（打错名字、少装依赖）换成 `None`，表现为「某个 client 突然是 None」而不是 import 就炸。同款写法在 [20260705_STREAM_READER_UNIFY_CLEANUP.md](20260705_STREAM_READER_UNIFY_CLEANUP.md) §5 已因同一理由删过一次。

`test_import_hygiene.py` 的 `app.services.step_store` 耗时上限**仍取 1.0**（与其他服务包一致，不改成贴着实测值的紧上限——机器负载下抖动会变噪声源），只把注释里的过路费说明换成删除后的实测值。

**（b）`settings.py` 的 docstring / 注释按 §11 同款判据过一遍**（代码零改动，AST SAME）。删掉与本文及各服务 docstring 重复的论证（storage_dir 的「三方都读」、`inference_fps` 的派生推导），改掉一处已过期的口径：`storage_base_dir` 原写「persistence / inference / traceback 三方都读此值」，收口后它**只对 step_store 可见**，由门禁 `test_storage_root_is_private_to_step_store` 锁死——照原文去写就会撞门禁。

---

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 段文件名正则 | 2 份拷贝 | 1 份 |
| sidecar 命名 | 写侧 f-string、读侧 `with_suffix` 反推，无关联 | 同模块内互为逆运算 |
| init / playlist / metadata 文件名 | 12 处硬编码（含 3 处**错误消息**里的字面量 —— 改名后它们会指向不存在的文件，且没有任何测试能发现） | 全部经 `PRODUCTS` |
| EXTINF 解析 | 2 份，容忍度靠巧合一致 | 1 份，成对解析，写侧读侧共用 |
| VOD m3u8 | 3 份构造 + 3 份备料 | 1 副骨架（包内）+ 1 个成品出口 |
| 段二分定位 | 2 份，off-by-one 各论证一遍 | 1 份，论证只写一处 |
| `routers/traceback.py` 的格式逻辑 | 102 行 | 12 行（只剩状态码、token、单位换算） |
| 门面成员 | 17（含 `base_dir` / `task_dir` / `durations` / `has_init` / …） | **5 个模块级函数**，门面类本身删除（§10） |
| 存储根的注入形参 | 7 个消费类各一个 `store=` / `base_dir=` | **0**，一律自解析（§10） |
| step 粒度类型 | 2（`StepRef` 摘要 + 一堆带 `(t,s)` 参数的方法），**时间口径不一致** | **1**（`Step` 句柄，清单直接返回它，零额外成本） |
| `SegmentRef` 字段 | 6 | **2** + 2 个派生 |
| traceback playlist 每请求扫盘 | 段列表 / EXTINF / init 各扫一遍 | 1 次 `iterdir` |
| `settings.storage_base_dir` 跨包直读 | 5 处 | **0**，门禁锁死 |
| `inference` / `lab` 的依赖方向 | 依赖 `traceback`（业务语义包） | 依赖 `step_store`（leaf） |
| step 目录删除者 | 2 处各自 `rmtree`，其一自述与行为不符 | 唯一入口，docstring 如实声明 |
| 「这目录里有谁的东西」 | 无处可查 | `PRODUCTS` 9 类，忘登记即 `KeyError` |
| TTL 判据 | `metadata.json.updated_at`（HLS 独有产物） | 已登记产物 mtime 最大值；无 HLS 的 step 不再泄漏 |
| rmtree → open_fresh 顺序约束 | 只活在 3 行注释里 | 三方 docstring 互指 |
| `cleanup_worker` 测试覆盖 | **0** | 17 个用例 |
| leaf 地位 / 存储根私有 / 写者白名单 | 无此概念 | 4 条 AST 门禁锁死 |

## 自测结果

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | **519 passed**（基线 485 + 门禁 3 + `test_step_store_api.py` 扩充 31） |
| 接口收口阶段断言值 | **零修改**（只改构造与成员名）；`/task/history`、`/traceback/*` 响应逐字节不变 |
| **端到端 round-trip** | **13 / 13 PASS**（`integration_tests/test_frame_lookup_roundtrip.py`）。含 T1「1800 帧 ts 位级相等 + 像素 id 逐帧匹配」—— 它直接验证 `product_path` 产出的文件名与 sidecar 命名与改造前逐字相同，否则解码侧根本找不到段 |
| VOD m3u8 逐字对账 | `TestVodPlaylist::test_full_text` 断言整段文本；token 化形态由 `test_traceback_router` 覆盖 |
| 新旧 TTL 判据对照（真实数据） | 本地 `database/9001/{1,2}`，在 `cleanup_days` = 7 / 25 / 30 三个阈值下**结论完全一致** —— 改判据对现有数据零行为差异，差异只出现在旧判据看不见的目录上 |
| dry-run | `StorageCleanupWorker(dry_run=True)` 跑通，确认未删任何目录 |
| 命名逐字对账 | `layout.*` 产出与改造前字面量逐字相同，含截断边界（`0.9999999 → 999999`，验证截断而非四舍五入） |
| 段二分等价性 | 8 个段级裁剪用例全过，含「起点恰为段首帧」「区间早于首段」两个 off-by-one 用例 |
| 读侧不留痕 | 手工验证：`FeatureStore` / `FactLedger.load` 读不存在的 step 后存储根下零残留 |
| 导入门禁 | 12 个用例全绿；`import app.services.step_store` 重依赖集合为空 |
| 未用 import | AST 扫描 0 残留 |

> 该集成测试**不需要 RTSP、不碰数据库**（直接调 `HLSPersistenceStrategy` 走真实写路径，只需 ffmpeg），写 `database/9900002/` 且结束即自清理。
> Windows 下须加 `PYTHONIOENCODING=utf-8` —— 脚本里的 ✅ 在 GBK 控制台会 `UnicodeEncodeError`（既有环境问题，与本次改动无关）。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **新 TTL 判据会开始回收此前泄漏的目录**（只有 `features.jsonl` 无 `metadata.json` 的那类） | 首次生效时可能一次性删掉一批历史目录。本地无此类目录，故对照验证覆盖不到这条路径 | 部署到有历史数据的环境前，**先跑一轮 `dry_run=True`** 核对清单 |
| `lab/clip_builder` 仍走包内私有骨架 | 唯一没进 `Step.vod_playlist` 的调用方 | 待验证「相邻段 ts 跨度 ≈ playlist EXTINF」在 fps 漂移下是否成立。退出条件已写进门禁例外的注释 |
| 「无任何已登记产物」的目录永不回收 | `last_activity_at` 返回 `None` 时选择**不删** —— 判不出是「刚建还没写」还是「写完被清空」，误删代价高于留一个空目录。空目录不占空间，且其 task 父目录仍会被 `sweep_empty_tasks` 收走 | 已接受。真要清理需另加「建目录时刻」的记录 |
| `segment_decoder` 让本包有了第一个会起子进程的模块（看门狗线程 + 临时文件 stderr） | 「这个包会不会起进程」的认知负担变了 | 已在 `__init__.py` 与 `Step.frames` docstring 显式标注。依赖等级不受影响（`subprocess` 是 stdlib、`numpy` 是 L1），leaf 门禁照常绿 |
| ~~`app/services/__init__.py` 顶层 `from .client import client_manager`，使**每个** `app.services.*` 子模块的 import 都付 0.28s 过路费~~ | 正是[包结构规范](20260903_PACKAGE_LAYOUT_SPEC.md) §3/§5 明令禁止的形态，只是门禁未覆盖 `app.services` 这一层 | **已处理**，见 §14(a)：该 re-export 零消费方，直接删除（`app.services` 0.303s → 0.002s）。「门禁未覆盖 `app.services` 这一层」仍成立，未加新门禁 |
| `traceback` 包搬空后只剩 `media_token.py` | 是否降为裸模块 `app/services/media_token.py`（先例 `run_control.py`） | 未决，避免搅进本次改动面 |
| `dry_run` 未接入配置文件 | 运维无法从 yaml 开启 | 刻意不做 —— 判据换代时的一次性验证工具 |
| `Timeline._build_cmd` 的 init 段此前硬编码 `raw_init.mp4` | `track="processed"` 下原会拿 raw 的 init 解 processed 段（SPS/PPS 不匹配）。生产代码与测试均只走 `track="raw"`，故无实际影响 | 已随命名收口一并修正，属**顺带修掉的潜在错误**，非行为回归 |
