# 抽取 `step_store`：step 目录落盘格式独立成 leaf 包

> **变更状态**：进行中（2026-09-06）　<!-- 四期计划，期 1 已落地；期 2-4 见文末「后续任务」 -->
> **知识库**：待沉淀
>
> <!-- 期 1 为纯搬运 + 命名收口，零行为变更；期 4 含唯一的行为改动，届时单独 PR -->

## 概述

`database/{task_id}/{step_id}/` 的落盘格式知识此前散在 4 个服务包 + 4 个 router，实测 12 份重复副本，并由此产生三条缺陷。本次抽出 `app/services/step_store/` 作为零跨服务依赖的 leaf 包，写侧读侧共同向它依赖。期 1 已落地：搬 `SegmentFinder`、建命名真源 `layout.py`、改 19 处 import、加 2 条门禁。

## 变更背景

### 现状 / 痛点

**12 份重复副本**（调研实测）：VOD m3u8 构造 ×3、EXTINF 解析 ×2（且手法不同）、段文件名正则 ×2、段二分定位 ×2、init.mp4 存在性判据 ×2（代码注释已自认是复制）、ffmpeg 起停 ×4、`finder or SegmentFinder(...)` 构造样板 ×3。

**重复全在读侧**。写段本身（tfdt hex-patch / timescale pin / ffmpeg 4.x-8.x cwd 兼容）只有一份，无重复。

三条由此产生的实际缺陷：

| 编号 | 问题 | 风险 |
|------|------|------|
| P1-① | `purge_step_dir` 的 docstring 列举它「只删 HLS 产物」，实际 `rmtree` 整个 step 目录，连带删掉 `inference` 写的 `features.jsonl` / `facts.jsonl` / `offline_inference_result.json` | 删除者的自述与行为不符 |
| P1-② | `persistence.start_run`（rmtree）必须先于 `FeatureStore.open_fresh`（建文件），否则新文件被抹。约束的**全部载体**是 [run_control.py:89-93](../../app/services/run_control.py) 的三行注释，两侧代码互不知情 | 顺序一动即静默丢数据 |
| P1-③ | `cleanup_worker` 只 `glob("*/*/metadata.json")` 判 TTL。有 inference 产物但无 HLS 产物的 step 目录永远扫不到 | `features.jsonl` 确定性泄漏 |

### 为什么现在的位置放不下

- **`persistence` 的定位是慢任务异步处理**（队列 + WorkerPool + strategy 分发）。`hls_strategy` 只是恰好被异步 worker 调用而寄生其中。
- **`services/traceback/` 是业务语义包**（告警回溯取证），却被 `inference` 和 `lab` 依赖，只为拿一个落盘布局解析器。
- **写侧不敢依赖读侧**：`hls_strategy` 为算 tfdt 偏移要回读自己写的 playlist，但用 `traceback` 那份解析器会造成 `persistence → traceback` 的别扭方向，只好自己再写一遍。**这批重复是当前布局逼出来的，不是谁偷懒。**

### 触发来源

从「`FrameTracker` 为什么要延伸这么多内部类」这个问题起，逐层追问到公共工具与模块私有的划分，最终定位到 `database/` 读写操作缺一层抽象。

### 承接

沿用[包结构规范](20260903_PACKAGE_LAYOUT_SPEC.md)的条文与 playbook（§1 文件角色、§2 依赖分级、§3 `__init__` 两种形态、§7 门禁；期 3 的脚本批量重写手法）。本次为它新增一个包，规范本身需在期 1 合入时补一条（见「后续任务」）。

## 方案详情

### 全景：格式知识从「各家自备」变成「共同下游」

```text
改造前                                  改造后
  persistence/hls_strategy  ─┐            persistence/hls_strategy ─┐
    自建段名正则、EXTINF 解析  │                                      │
  traceback/segment_finder  ─┤            traceback（只剩 MediaToken）│
    段名正则、EXTINF 解析      │            lab/clip_builder          ─┼─▶ step_store（leaf）
  lab/clip_builder          ─┼─ 各写各的  lab/step_exporter          ─┤     layout   命名真源
  lab/step_exporter         ─┤            inference/offline          ─┤     finder   段定位
  inference/offline         ─┤            routers/*                  ─┘     playlist EXTINF
  routers/*                 ─┘
                                        step_store 不 import 任何 app.services.*（门禁锁死）
```

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| 期 1：建包 + 搬 SegmentFinder + 命名真源 | `app/services/step_store/` | §1-§6 |
| 期 2：格式知识去重 + router 下沉 | 同上 + `routers/traceback.py` | §7-§10 |
| 期 3：段解码搬迁 + 正名 | 同上 + `inference/offline/` | §11-§12 |
| 期 4：删除入口收口 + 产物注册表 | `step_store/purge.py` | 后续任务 |

**边界（写进 `__init__.py` docstring，防后人往里塞）**：异步调度、**TTL 的调度与策略**、写侧 ffmpeg 转码参数、`FeatureStore` 的批缓冲与 owner fence、`MediaToken`、各家的 ffmpeg cmd、HTTP 状态码与 token 化 URL —— 全部留在原处。

> **TTL 的职责切分**：`persistence` 保留「保留天数、扫描周期、删不删」的全部决定权；`step_store` 只回答「这目录里有什么、最后何时活动」。故新模块叫 `purge.py` 而非 `lifecycle.py`——后者会被读成它接管了目录生命周期。

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **薄层：布局 + 删除入口（采用）** | ~400 行新包，改 19 处 import | 采用。三条缺陷全出在「路径/命名/删除」这一薄公共面上，没有一条出在「内容怎么写」上 |
| 厚层 Repository / DAO | 要搬走批缓冲、目录锁、owner fence、ffmpeg 转码 | 否。这不是"抽一层"，是重写 persistence 与 inference/feature |
| 只去重不建包 | 写侧仍不敢依赖读侧 | 否。重复正是当前布局逼出来的，不动布局就会重新长出来 |

### 1. `app/services/step_store/layout.py`（新增）— 命名的唯一真源

段文件名正则此前有两份拷贝（读侧 named group、写侧位置 group），路径拼接则是写侧读侧各自硬编码。

**风险最高的是 sidecar 那一对**：写侧 f-string 拼 `raw_segment_{ts_us}.idx`、读侧 `with_suffix(".idx")` 反推，靠「stem 长一样」这个隐式契约对上。命名一旦不一致，读侧 `_load_sidecar` 按契约只 warning 跳过该段（那个宽容本身是对的），表现为**静默丢帧**而非失败。

对外符号：`SEGMENT_PATTERN` / `VALID_TRACKS` / `METADATA_NAME` / `ts_to_us` / `step_dir` / `segment_name` / `sidecar_name` / `sidecar_name_for` / `init_name` / `playlist_name` / `parse_segment_name`。

依赖上界 stdlib only（L0），不 import settings、不 import numpy——任何人都该能零成本拿到命名。

> `ts_to_us` 的**截断而非四舍五入**是既有落盘约定的一部分，不能改：读侧按 ts 定位段依赖 `ts_us <= ts*1e6`（这正是段级二分必须用 `side='right'` 再减一的原因）。改成 round 会让「start_ts 恰为该段首帧」的定位无条件出错。

### 2. `segment_finder.py` → `step_store/finder.py`（`git mv`，保留历史）

搬走 `SegmentFinder` / `SegmentRef` / `StepRef` / `get_default_base_dir`；`parse_playlist_durations` 去 `playlist.py`。`traceback` 包只剩 `media_token.py`——token 是访问控制，不是落盘格式。

### 3. `step_store/playlist.py`（新增）— EXTINF 解析

本期只搬读侧那份，并把它拆成两个函数：

- `parse_playlist_entries()` 返回**有序** `[(filename, dur)]`
- `parse_playlist_durations()` = `dict(entries)`，保持原签名与语义

保序是为期 2 铺路：写侧 `_ts_offset_seconds` 要的是「当前段以前所有 EXTINF 的前缀和」，顺序即语义；读侧要的是映射。一份解析供两种消费。

### 4. 引用点改写（19 处）

两种现存写法统一成深路径：`from app.services.traceback import SegmentFinder` 与 `from app.services.traceback.segment_finder import ...` 均改为 `from app.services.step_store.finder import ...`。覆盖 `routers/{traceback,media,task,lab}.py`、`services/lab/{clip_builder,step_exporter,config}.py`、`services/inference/offline/frame_tracker.py`、`tests/` 与 `integration_tests/` 对应文件。

同时把各消费点的硬编码落盘名换成 `layout.*`——**包括错误消息里的字面量**（`f"{track}_init.mp4 not found..."` 等 3 处）：改名后它们会指向一个不存在的文件名，且没有任何测试能发现。

### 5. 门禁（`tests/test_import_hygiene.py`）

- `app.services.step_store` 进 `BUDGET`，重依赖集合为空。
- **新增 `test_step_store_is_a_leaf`**：AST 遍历本包全部 `.py`，禁止 import 任何 `app.services.*`（本包内部互引除外）与 `app.routers`。破这条即 leaf 地位失守，写侧读侧的循环依赖会重新长出来。

### 6. 保留项（刻意不改）

- **落盘文件名与格式**：一个字节不动，`layout` 生成的字符串与改造前逐字相同。
- **`MediaToken`** 留在 `services/traceback/`。
- **`app/services/__init__.py` 的 `client_manager` re-export** 不动（见「遗留风险」）。
- **各家 ffmpeg cmd 与失败策略**：五处完全不同，是真私有，期 3 也只收「怎么起、怎么报错」。

---

## 期 2：格式知识去重 + router 下沉

### 7. EXTINF 解析统一（2 份 → 1 份）

写侧 `_ts_offset_seconds` 自带 `_EXTINF_RE` 正则求前缀和，读侧 `parse_playlist_durations` 用字符串切法出映射。现统一到 `playlist.parse_playlist_entries()`（保序 `[(filename, dur)]`），两侧各取所需：

| 消费方 | 现在调 |
|--------|--------|
| `hls_strategy._ts_offset_seconds`（tfdt 起点） | `playlist.sum_durations()` |
| traceback / step_exporter（时长真值 + 在途判据） | `playlist.parse_playlist_durations()` |

> **一处行为差异（更严格，非回归）**：旧正则对**孤立** `#EXTINF:` 行（后面没跟文件名，只可能出现在 append 写到一半崩溃时）也计入求和；新解析器要求 EXTINF 与文件名成对才计入。孤立条目意味着那个段并未真正进入 playlist，**不计入才是对的**——旧算法会让该段之后所有 fragment 的 tfdt 偏移偏大。两者对畸形行（`#EXTINF:1.5,title`）的处理一致，均跳过。
>
> 计划里原写「统一后以正则版为准，它更严格」，实际相反：成对版更严格，且更正确。

### 8. VOD m3u8 骨架统一（3 份 → 1 副骨架 + 3 份备料）

`playlist.build_vod_playlist(entries, map_uri, target_duration, media_sequence=0)`。**只共用骨架，不统一语义**——三处的真差异原样保留：

| 调用方 | 段 URI | EXTINF 来源 | TARGETDURATION | MEDIA-SEQUENCE |
|--------|--------|------------|----------------|----------------|
| `traceback._build_vod_playlist` | token 化 URL | playlist EXTINF | `round` | 0 |
| `step_exporter._build_vod_text` | 同目录 basename | playlist EXTINF | `ceil` | 0 |
| `clip_builder` | 同目录 basename | **相邻段 ts 跨度** | `int+1` | **不写** |

`clip_builder` 的 EXTINF 来源不同是刻意的：它的 seek 基准是 ts，改用 playlist EXTINF 会让逐段 seek 错位（原注释已论证）。故骨架只吃 `[(uri, duration)]`，备料留调用方。

**三种 TARGETDURATION 算法一并原样保留**，其中 `round` 那处有规范问题，见「遗留风险」。

### 9. 段二分统一（2 份 → 1 份）+ 去掉一个 numpy 数组

`SegmentFinder.find` 的 `bisect_right - 1` 与 `Timeline.iter` 的 `np.searchsorted(..., 'right') - 1` 是同一个原语：**找最大的 i 满足 `seg_ts_us[i] <= target`**。抽成 `finder.locate_containing_index()`，off-by-one 的论证（为何不能用 `bisect_left`：既取到 target 之后的段，又因 ts_us 是截断值而漏掉「target 恰为段首帧」的情形）现在只写一份。

**返回 -1 由调用方处理，不在函数内 clamp**——两个消费方的需求正好相反：

- 段级区间裁剪（`Timeline` 的 `hi`）要靠 -1 表达空区间，clamp 成 0 会把空区间误判成命中第 0 段；
- 告警取证（`SegmentFinder.find`）要 clamp 到首段，取最近的可用段。

`Timeline` 的 `self._timestamps`（float64 ndarray）随之删除，改持 `List[int]`。**与 numpy 版严格等价**：`seg_ts_us` 元素为整数时 `a <= t ⟺ a <= floor(t)`，故传 `ts*1e6` 浮点原值与传截断值同结果，无需取整。段数为百量级，bisect 与 numpy 无性能差异（后者还多一次数组构造）。

### 10. router 下沉（102 行 → 41 行）

判据与计算进 service，协议层决定留 router：

| 下沉的 | 去了哪 | router 留下什么 |
|--------|--------|----------------|
| init 存在性判据 | `playlist.has_init()` | 映射成 HTTP 503 |
| 在途段过滤（与 step_exporter 重复） | `playlist.filter_playable()` | 空结果映射成 404 |
| m3u8 构造 | `playlist.build_vod_playlist()` | token 签发、`base_url` 拼装 |
| 双轨 EXTINF 时长聚合（`_step_duration_ms` 32 行） | `playlist.step_time_bounds_us()` | µs → ms 换算（对前端的表示口径） |

`playlist.py` 对 `finder` 的类型引用走 `TYPE_CHECKING`，避免包内模块级互引。

---

## 期 3：段解码搬迁 + 正名

### 11. `Timeline` → `step_store/segment_decoder.SegmentDecoder`

**搬迁的理由不是复用**——`FrameTracker` / `Timeline` 在生产代码里**零调用方**（只有 tests 与 integration_tests 用），唯一既定消费者是 ROI 三期。理由是 **sidecar 被劈开了**：

```text
.idx 这一个文件，期 1 后归三个包管
  写内容   hls_strategy._update_timeline        persistence
  命名     layout.sidecar_name / _name_for      step_store     ← 期 1 搬来
  读内容   Timeline._load_sidecar               inference/offline
           + 段内帧号 n ↔ 下标 k 的 1:1 对齐
```

那个 1:1 对齐是整个索引的地基（靠不拼 m3u8、不用 `-ss`、`-vsync 0` 保证，破了不报错、只静默取错帧）。写侧读侧隔着两个包，改一侧看不见另一侧。搬完名字与内容同居本包，与写侧只隔一层。

`git mv frame_tracker.py → step_store/segment_decoder.py`：190 行的解码实现带着大量踩坑注释，历史跟着大头走。

### 12. `FrameTracker` → `inference/offline/frame_finder.FrameFinder`

**留在 offline**：「ts 位级相等对号、配不上就 ValueError」是消费侧策略，不是格式知识。与 `SegmentFinder` 成一组对仗——**段级找段、帧级找帧**。

两者的失败契约相反，这正是它们分居的理由：

| | 契约 | 缺数据时 |
|---|------|---------|
| `SegmentDecoder.iter` | 区间扫描，**宽容** | 缺 sidecar 跳过该段、空区间返回空 |
| `FrameFinder.find` | 点查，**严格** | 任一 ts 配不上就 `ValueError` |

新增 `decoder` 注入口，测试不必再 monkeypatch 模块属性（`monkeypatch.setattr(...frame_tracker.Timeline, Fake)` → `FrameFinder(..., decoder=Fake(...))`）。

> 采用 `FrameFinder` 而非[数据模型正名提案](20260906_OFFLINE_DATA_MODEL_NAMING.md) A 档写的 `FrameLocator`，该提案对应行已同步修订。

连带改名：`tests/test_frame_tracker_boundary.py` → `test_segment_decoder_boundary.py`，`integration_tests/test_frame_tracker_roundtrip.py` → `test_frame_lookup_roundtrip.py`（均 `git mv`）。两个被测对象分居两包但同居一份测试：共用同一个 seam，且拆开会让「宽容层 + 严格层」这对相反契约的用例互相看不见。

### 13. 撤回：不建 `app/utils/ffmpeg.py`

计划里原有一项「ffmpeg 起停工具 ×4 收口」（bin 解析 / 超时预算 / stderr 格式化），**评审后撤回**——三者都是过度包装：

```text
resolve_bin()         x or settings.ffmpeg_path，3 行
budget(floor,n,per)   把 max(floor, n*per) 重新命名了一遍
format_failure(...)   4 处输入形态本就不同（PIPE 的 str vs 临时文件的 fd）
```

**判据：重复值不值得抽，看它承载的知识显不显然。** 期 1-2 抽的每一样都带着一段论证、写错会静默出错（sidecar 逆运算、`bisect_right - 1`、EXTINF 不能用 ts 差重推、缺 ENDLIST 段全丢）；`max(floor, n*per)` 没有这种东西可论证，重复三遍不产生风险，抽出来也不消除风险。

同样撤回的还有 `finder or SegmentFinder(get_default_base_dir())` 那处（一行）。

> 顺带修正：上一轮把 stderr 截断长度 1500 / 500 / 全量的不一致当成缺陷提了——其实不是。三处面对的读者不同（异常给调用方、warning 给运维），不一致合理，不等于重复。

## 变更效果

| 维度 | 变更前 | 变更后（期 1-3 后） |
|------|--------|------------------|
| 段文件名正则 | 2 份拷贝（写侧 / 读侧各一） | 1 份，`layout.SEGMENT_PATTERN` |
| sidecar 命名 | 写侧 f-string 拼、读侧 `with_suffix` 反推，无关联 | `sidecar_name` / `sidecar_name_for` 同一模块内互为逆运算 |
| init / playlist / metadata 文件名 | 12 处硬编码（含 3 处错误消息） | 全部经 `layout.*` |
| EXTINF 解析 | 2 份，正则 vs 字符串切，容忍度靠巧合一致 | 1 份，成对解析；写侧读侧共用 |
| VOD m3u8 构造 | 3 份，8 行头部逐行重复 | 1 副骨架 + 3 份备料，真差异原样保留 |
| 段二分定位 | 2 份，bisect vs np.searchsorted，off-by-one 各论证一遍 | 1 份 `locate_containing_index`，论证只写一处 |
| init 判据 / 在途段过滤 | 各 2 份（注释已自认是复制） | 各 1 份 |
| `routers/traceback.py` 的格式逻辑 | 102 行 | 41 行（只剩状态码、token、单位换算） |
| sidecar 的名字与内容 | 命名在 step_store、内容解释在 offline，隔两包 | 同居 step_store，与写侧只隔一层 |
| `Timeline` / `FrameTracker` | 名字像数据结构 / 像目标跟踪，且与三期 `SlotTracks` 撞词 | `SegmentDecoder` / `FrameFinder`，后者与 `SegmentFinder` 成对仗 |
| `inference` / `lab` 的依赖方向 | 依赖 `traceback`（业务语义包） | 依赖 `step_store`（leaf） |
| leaf 地位 | 无此概念 | 由 `test_step_store_is_a_leaf` 锁死 |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | **453 passed**（基线 451 + 2 个新门禁用例），**零断言值修改**，期 1/2 各阶段共跑 7 轮 |
| 导入门禁 | `app.services.step_store` 重依赖集合为空 |
| 命名逐字对账 | `layout.*` 产出与改造前字面量逐字相同，含截断边界（`0.9999999 → 999999`，验证截断而非四舍五入） |
| m3u8 逐字对账 | 三种形态（token URL / basename+MEDIA-SEQUENCE / basename 无 MEDIA-SEQUENCE）输出与改造前逐字相同 |
| 段二分等价性 | `test_frame_tracker_boundary.py` 的 8 个段级裁剪用例全过，含「起点恰为段首帧」「区间早于首段」两个 off-by-one 用例 |
| 误伤对账 | 代码中已无硬编码落盘名残留；未使用 import 已清（`re` / `Path` / `np.searchsorted`） |
| **端到端 round-trip** | **13 / 13 PASS**（`integration_tests/test_frame_lookup_roundtrip.py`，期 3 后跑）。含 T1「1800 帧 ts 位级相等 + 像素 id 逐帧匹配」——这是唯一能抓「解出来的是不是那一帧」的手段，直接验证搬迁后 `n ↔ k` 的 1:1 地基仍成立；T11 缺 sidecar 降级只丢该段 150 帧 |

> 该集成测试**不需要 RTSP、不碰数据库**（直接调 `HLSPersistenceStrategy` 走真实写路径，只需 ffmpeg），写 `database/9900002/` 且结束即自清理。
> Windows 下须加 `PYTHONIOENCODING=utf-8`——脚本里的 ✅ 在 GBK 控制台会 `UnicodeEncodeError`（既有环境问题，与本次改动无关）。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **新发现（本次未修）：`traceback` 的 `EXT-X-TARGETDURATION` 用 `round`，可能违反 RFC 8216 §4.3.3.1** | 该字段必须 **≥ 每个 EXTINF 的向上取整值**。三个消费方的算法是 `round`（traceback）/ `ceil`（step_exporter）/ `int+1`（clip_builder）——后两者恒安全，`round` 在「最长段时长小数部分 < 0.5」时会取到比实际段长小的整数（如 max EXTINF = 10.4 → 声明 10）。hls.js 通常宽容，但原生 HLS 播放栈（Safari / AVPlayer）可能拒绝；而该 router 恰恰专门为原生栈补过 HEAD 路由，说明它在服务范围内 | **需拍板**。改 `round` → `ceil` 是一行，但属行为变更（期 2 承诺零行为变更），故本期只把三种算法**原样**参数化保留、不擅自统一。建议单独一个小 PR 修，并在 dev 上用真实段长确认当前是否已经踩中 |
| `segment_decoder` 让 step_store 有了第一个会起子进程的模块（看门狗线程 + 临时文件 stderr） | 本包此前是纯函数 + 无 I/O 的 `SegmentFinder`，「这个包会不会起进程」的认知负担变了 | 已在 `__init__.py` docstring 显式标注该模块**会起 ffmpeg 子进程**。依赖等级不受影响（`subprocess` 是 stdlib、`numpy` 是 L1），leaf 门禁照常绿 |
| **期 4**：删除入口收口 + 产物注册表 + 修 P1-①②③ | 唯一有行为变更的一期 | 单独 PR。`cleanup_worker` **目前零单测覆盖**，先补 `tests/test_step_store_purge.py` 再改判据；dev 上先 dry-run 核对会删哪些目录，再开真删 |
| **新发现（本次未修）**：`app/services/__init__.py` 顶层 `from .client import client_manager`，使**每个** `app.services.*` 子模块的 import 都付 0.28s 过路费 | 实测 `import app.services` 0.303s，而 `import app.services.step_store` 0.275s——即本包自身成本≈0，全是这笔过路费。正是规范 §3/§5 明令禁止的形态（单例应在 `instance.py`、`__init__` 不 re-export），只是门禁未覆盖 `app.services` 这一层 | 另案处理，不混进本次改动面。step_store 的耗时上限故取 1.0 与其他服务包一致，注释写明原因 |
| `Timeline._build_cmd` 的 init 段此前硬编码 `raw_init.mp4`，本次改为 `layout.init_name(seg.track)` | `Timeline(track="processed")` 下原会拿 raw 的 init 解 processed 段（SPS/PPS 不匹配）。生产代码与测试均只走 `track="raw"`，故无实际影响 | 已随命名收口一并修正，属**顺带修掉的潜在错误**，非行为回归 |
| `traceback` 包搬空后只剩 `media_token.py` | 是否降为裸模块 `app/services/media_token.py`（先例 `run_control.py`） | 留到期 4 之后单独决定，避免搅进改动面 |
| [包结构规范](20260903_PACKAGE_LAYOUT_SPEC.md) §1 子包分类表与 §6 引用面条款未提 `step_store` | 新包缺规范位置 | 合入时补：`step_store` 是继 `client_manager` 之后第二个「零跨服务依赖、谁都可向下依赖」的 leaf |
