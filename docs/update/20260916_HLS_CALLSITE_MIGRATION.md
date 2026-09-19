# HLS 读写调用点一次性迁移：产物落位换成 `{step}/hls/`，写侧接线到 recording

> **变更状态**：生效中（2026-09-16）　<!-- 读侧 10 个调用点 + 写侧编排全部切到数据层；旧实现留在仓库里但已无调用点、不再启动 -->
> **知识库**：待沉淀
>
> <!-- storage 分层第 4 期的阶段 2 + 4 + 5（MAP §5.1）。阶段 1（回落层）取消，阶段 6（删旧代码）推迟 -->

## 概述

HLS 产物落位从 `{task}/{step}/` 平铺换成 `{task}/{step}/hls/`。读侧 10 个调用点（4 个 router +
`lab` 三件 + `frame_tracker`）从 `services/traceback/segment_finder` 切到 `app.storage.hls` /
`app.storage.tasks`，VOD 清单骨架收敛到 `app/services/utils/vod_playlist.render_vod`；写侧编排从
`persistence` 的四件套切到 `app/services/recording`，TTL 判据同批从 `metadata.json` 的 `updated_at`
换成 `{step}` 目录自身的 mtime。旧实现一个字没删，只是不再有人调、不再启动。

## 变更背景

### 现状 / 痛点

`app/storage/hls`（读 + 写）与 `app/services/recording` 在 2026-09-11 → 09-12 已全部落地并有单测，
但**一个调用点都没迁**——新代码全是死代码，运行时行为与迁移前完全一致，七对双份实现并存
（MAP §4），改其中一份不会让另一份红。本轮是这条链路的价值兑现点。

### 四条前置决策（两条推翻了 MAP 里的既有立场，落地时按新的走）

```text
① 不做回落层（阶段 1 取消）    推翻 §6.1「临时读侧路径回落」——读侧只认 {step}/hls/
② 老数据不管，随 TTL 自然消失   故 §4 的 TTL 判据换代必须同批，否则这条不成立
③ 旧代码不删（阶段 6 推迟）    接线点切走，类与文件留着；旧测试因此继续绿
④ 转码 / tfdt 失败 = 整段作废   §6.3 待拍板 #1 定案，`_write.py` 保持现状不改
```

### ①带来的结构性后果：读侧与写侧必须同一批上线

没有回落层，任何一侧单独上线系统都处在「什么都读不到」的状态：

```text
只上读侧   读 {step}/hls/，盘上是平铺      → 回放 / 送标 / 离线全空
只上写侧   写 {step}/hls/，读的是平铺      → 连新录的也读不到
```

省掉一个临时层 + 它的删除条件 + 三条门禁，代价是失去「每阶段单独上线与回滚」。**回滚粒度是整批。**

### 三个必须同批处理的耦合点

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | 两个 sweeper 都从活跃 CQ **drain**（破坏性取出） | 同时在跑 = 各自拿走一半帧，产出两份互相缺帧、时间轴却都自洽的段，**两端都不报错** |
| #2 | `metadata.json` 随写侧迁进 `{step}/hls/`，TTL 的 `glob("*/*/metadata.json")` 匹配不到 | **老数据照常回收、新数据永不回收**——单向漏盘且无任何日志，比整个停摆更难发现 |
| #3 | `start_run` 是 eager rmtree 整个 step 目录（跨三域、跨线程零互斥） | 与 recording 的域内懒惰首写自清两套 supersede 同时在跑，新 run 的产物可能被上一套删掉 |

### 承接

- 编排与代次语义取自 [20260911_RECORDING_SERVICE](20260911_RECORDING_SERVICE.md)：一条
  `SerialTaskQueue`（提交序 = 执行序）+ 出队时代次判等 + 本代次首写自清，全系统零锁。
- 读侧能力与 VOD 出域取自 [20260912_HLS_READ_CAPABILITY](20260912_HLS_READ_CAPABILITY.md)。
- TTL 判据取自 MAP §6.2：Linux + Python 3.11 拿不到真正的创建时间（`st_birthtime` 不存在、
  `st_ctime` 两平台语义不同），故以 `{step}` 目录自身的 mtime 作代理。

## 方案详情

### 全景：一条写链 + 三片读链 + 一个判据换代

```text
写   [推理线程] 结果帧 ──▶ ClientQueues 缓冲
                               │
        ┌──────────────────────┴───────────────────────┐
        │ 旧：HLSSegmentSweeper → hls_queue →           │ ← ① start() 不再启动它
        │     HLSWorkerPool → hls_strategy → {step}/    │    （构造照留，代码一字未动）
        └──────────────────────────────────────────────┘
                               │
     新：recording.SegmentSweeper → SerialTaskQueue → hls.insert_segment → {step}/hls/

     main.py     health_monitor > stream > persistence > recording > inference   ← ②
     run_control start: 删 start_run       stop: flush_residual → forget_task    ← ③
     TTL         {step}/ 自身 mtime > cleanup_days → rmtree                      ← ④

读   routers   hls.list_playable_segments / list_segments_by_track / parse_*     ← ⑤
               + tasks.list_task_ids / list_step_ids + render_vod
     lab 三件  同上；clip_builder 只换容器类型、不换时长口径                      ← ⑥
     offline   FrameTracker.find → hls.iter_frames                               ← ⑦
```

三条硬顺序，都不是风格问题：

```text
读侧与写侧同批上线             没有回落层，单边上线 = 什么都读不到（见变更背景）
recording 必须嵌 inference 外层 inference.stop() 经 run_control 交出最后一批残段，那时队列
                               必须还活着；队列先停 = 帧已出 CQ、提交被拒 = 真丢
flush_residual 早于 forget_task 首写自清以「current is job.cq」为前提；反序则残段执行时代次表
                               已空而 CQ 仍是现任 → 把刚写完的整段录像删掉再写
```

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| ① 停掉旧 HLS 四件套的启停 | `app/services/persistence/manager.py` | §1 |
| ② 嵌 `recording.lifespan()` | `app/main.py` | §2 |
| ③ start / stop 三处改调 | `app/services/run_control.py` | §3 |
| ④ TTL 判据换代 | `app/services/persistence/workers/cleanup_worker.py` | §4 |
| ⑤ 4 个装配层调用点 | `app/routers/{media,traceback,task,lab}.py` + `services/traceback/__init__.py` | §5 |
| ⑥ lab 三件 | `app/services/lab/{step_exporter,clip_builder,config}.py` | §6 |
| ⑦ 离线反查 | `app/services/inference/offline/frame_tracker.py` | §7 |
| 刻意保留项 | — | §8 |

### 1. `persistence/manager.py` —— 摘启停，不摘构造

`start()` / `stop()` 里去掉 `hls_pool` 与 `_segment_sweeper` 的启停，`alarm_pool` 与
`_cleanup_worker` 照常。**`__init__` 里的构造原样保留**：直接 `PersistenceManager()` 再打桩的
既有测试（`tests/test_persistence_sink.py`）仍依赖这两个属性存在。

`start()` 的 docstring 写死了「重新启用 = 数据静默损坏」（#1）——本轮唯一一处「代码还在、
看起来能用、启用了就坏」的形状。

### 2. `app/main.py` —— 嵌在 persistence 同一档、inference 外层

```text
health_monitor > stream > persistence > recording > inference
```

理由与 persistence 完全相同，见全景的硬顺序。`recording/__init__.py` 的 `lifespan` docstring
原本就写着这个要求，本轮把该文件「## ⚠ 本期尚未接线」整节改写成已接线状态。

### 3. `run_control.py` —— 三处

| 位置 | 旧 | 新 |
|---|---|---|
| start（L94） | `persistence_manager.start_run(cq)` | **整行删除** |
| stop step 2 | `persistence_manager.flush_residual_segments(cq)` | `recording_service.flush_residual(cq)` |
| stop step 4 | `persistence_manager.release_task_locks(task_id)` | `recording_service.forget_task(task_id)` |

**`start_run` 是删不是改指**：它是 eager rmtree（整个 step 目录，含 `features/` / `lab/`），
recording 换成了**懒惰首写自清**（只清 `{step}/hls/`，且只在本代次真写出第一段时清）。语义不同，
没有对应方法可指。L89 那段讲 supersede 的注释一并更新——start 侧现在只剩
`inference.start_workflow` 里的 `FeatureStore.open_fresh` 一个钩子。

模块级 import 换成 `from app.services.recording.instance import recording_service`（走深路径，
不经包顶层 re-export），`persistence_manager` 在本文件已无用处，一并去掉。

### 4. `cleanup_worker.py` —— 判据换成 `{step}` 目录自身的 mtime，不下钻域子目录

```text
旧  glob("*/*/metadata.json") → 读 updated_at → 过期则 rmtree
新  iterdir 两级数字目录 → stat({step}).st_mtime → 过期则 rmtree
```

对平铺与分域两种布局一视同仁，于是决策②「老数据不管、随 TTL 自然消失」才站得住；同时消解历史
缺陷 #1（只有 `features.jsonl`、没有 HLS 段的 step 永不回收）。

**为什么 `{step}` 的 mtime 是创建时间的好代理**：它的直接子项只有 `hls/` / `features/` / `lab/`，
目录 mtime 只在**增删直接子项**时变。段落盘动的是 `hls/` 的 mtime，`{step}/` 纹丝不动——
`test_writing_a_segment_does_not_renew_the_step` 钉住这条。

三条实现上的取舍：

- **两级都只认十进制数字目录名**。旧判据靠「有没有 `metadata.json`」把存储根下的 `.lab_exports/`
  （lab 导出临时件，自带 30 分钟孤儿扫描）天然挡在外面；换成目录 mtime 后必须显式挡，否则一个
  15 天没动过的导出临时目录会被当成过期 step 删掉。
- **不复用 `app.storage.tasks.list_task_ids(order="mtime")`**：那个口径**下钻域子目录取最大值**，
  答的是「最近活动」不是「创建」——每写一段就续一次命，等于永不回收。两个口径分开是刻意的，
  `tasks.py` 的 docstring 也写着这一点。
- **删除动作保留 `shutil.rmtree`，不改用 `tasks.delete_step`**。`delete_step` 的路径经 `_root` 从
  `settings` 解析，而本 worker 扫的是构造时注入的 `self.db_dir`；生产上两者同源，但让删除动作认
  一个它自己没扫过的根是不必要的错位风险。代价是空 task 目录回收有两份实现，接受。

> ⚠ **两条偏差已写进 docstring**，否则后人会当漂移来查：① `{step}/lab/` 是**延迟创建**的，第一次
> 导出送标那一刻刷新 `{step}/` 的 mtime，该 step 的 TTL 计时重置——白捡的续命，不是 bug；
> ② **活跃 step 不再免疫**，见遗留风险。

### 5. `app/routers/*` —— 四个装配层调用点

**`media.py`：外部字符串不再进入路径拼接。** token 里的 `filename` 先 `hls.parse_segment_name` /
`hls.parse_init_name` 解成身份键（`SegmentRef` / track），解不出记 warning 并 400；路径由
`hls.segment_path` / `hls.init_path` 按结构重建。于是 `relative_to(base)` 那层事后越界检查**整个
删掉**——不是放松，是前移：能拼出来的路径只可能落在该 step 的 `hls/` 域目录里。原来 init 侧的
`endswith("init.mp4")` **放行 `evil_init.mp4`**，被完整匹配的正则取代（L2 的兑现）。

**`traceback.py`：清单文本不再手写。** `_build_vod_playlist` 只剩「把段与 init 换成 token 化 URI」，
事实（可播段 + 逐段 EXTINF）来自 `hls.list_playable_segments`，骨架来自 `render_vod`；
`_step_duration_ms` 同样改走 `list_playable_segments`（双轨各一次）。

> `get_task_playlist` 的两档错误**刻意保住**：`hls.list_segments` 空 → `NotFoundError`（这条轨根本
> 没段，多半是挑错 track）；`list_playable_segments` 空 → 404 `"No playable segments yet"`（段全在途，
> 稍后重试就有）。合成一档会让前端分不清。

**`task.py` + `lab.py`：step 摘要由调用方统计。** `StepRef` 在数据层刻意不出类型（只有一个完整
消费方）。`task.py` 新增私有 `_summarise_steps`，用 `tests/test_storage_hls.py::TestStepSummaryRecipe._summarise`
钉死的配方（有用例证明与 `list_steps` 逐字段相等）；`lab.py` 的 `_list_raw_steps` 是同一配方只取
raw 轨。顺带 `list_task_ids_by_recency()` → `tasks.list_task_ids(order="mtime")`。

> ⚠ **`tasks.list_step_ids` 不过滤空 step**（有无产物是域知识，TTL 要的正是没过滤那档）。旧
> `list_steps` 自带的「两轨都没段就丢弃」必须在调用点补回来，漏了就是大屏和送标页多出点开是黑屏
> 的条目。两处各有一条用例钉住。

**`services/traceback/__init__.py`：`__all__` 只剩 `MediaToken` / `MediaTokenError`。**
`SegmentFinder` 一并摘掉是有意的：留着它会让残留引用**静默拿到旧型**（旧 `SegmentRef` 6 字段、
新的 2 字段，混用不会立刻报错），而不是当场 `ImportError`。仍在用旧实现的 `frame_tracker` 与旧测试
都是直接 import 子模块 `...traceback.segment_finder`，不受影响。

### 6. `app/services/lab/*` —— 三件

| 文件 | 改动 |
|---|---|
| `step_exporter.py` | `list_segments` + `parse_playlist_durations` + 自滤在途段 → 一次 `hls.list_playable_segments`；`_build_vod_text` 整个删除换 `render_vod`；构造函数删 `finder` 形参 |
| `clip_builder.py` | 取数换 `hls.list_segments(t, s, "raw")`（**时长口径不动**）；手写骨架换 `render_vod`；`_validate_continuity(segs)` → `(spec, segs)`；构造函数删 `finder` 形参 |
| `config.py` | `_config_path()` 改直读 `settings.storage_base_dir`；`lab_runtime_config.json` 位置不变 |

**`clip_builder` 为什么不能用 `list_playable_segments`**：它的 EXTINF 取**相邻段 ts 差（实测墙钟）**，
不是清单里的 EXTINF。`-ss` 的 seek 基准是墙钟 ts，而 EXTINF 是 `帧数/raw_fps` 推出的恒定 10.000 s，
fps 漂移下累计 EXTINF 会偏离墙钟、逐段 seek 错位。所以这里**只换容器类型，不换时长口径**（完整理由
在 `app/services/utils/vod_playlist.py` 的模块 docstring）。代价是在途段照旧不被过滤（历史缺陷 #3），
按 MAP §2 归第 5 期独立评审，本次不修。

**临时清单为什么能落进域目录**：`.export_{nonce}.m3u8` / `.clip_{nonce}.m3u8` 匹配不上 `_layout` 的段名
与 init 名正则，读侧枚举天然跳过（与 `.stage_*/` 同一条理由）。段进 `{step}/hls/` 后清单必须跟着进去
——裸文件名的相对 URI 要解析到同目录的 init 与各段，ffmpeg 命令行因此一个字没改。目录由
`hls.init_path(t, s, track).parent` 取，**没有给 hls 域加新 API**：那个路径本来就要拿来做 `EXT-X-MAP`。

### 7. `frame_tracker.py` —— `FrameTracker.find` 改调 `hls.iter_frames`

`FrameTracker` 只留 `(task_id, step_id)` 两个字段，`track` 形参**删除**（新解码只服务 raw 轨；
`processed` 按契约不落 sidecar，给不出带墙钟 ts 的帧）。`find` 的对外契约（ts 升序、重复 ts 按重数
各产出一帧、位级 `==` 配不上就 `ValueError`）**一个字没改**——位级匹配按 MAP §3.3 的 🔒 留在 offline，
不进数据层。

`Timeline` 及其模块级私有件（`_read_exact` / `_DECODE_TIMEOUT_*`）**整体保留**，类 docstring 顶部标注
已退役。`tests/test_frame_tracker_boundary.py` 里测 `Timeline` 的那几组因此**原样不动、原样绿**，只重写
测 `FrameTracker` 的 3 条。

`integration_tests/test_frame_tracker_roundtrip.py` 选择**改而不删**：造数一旦切到 `hls.insert_segment`，
盘上只剩 `{step}/hls/`，`Timeline` 读平铺就什么都读不到——留着它要么再铺一套平铺数据给一份等着删的
实现续命，要么整半作废。那几项抓的是「ts ↔ 像素错配」，是纯 seam 单测**抓不到**的唯一一类错，在新
解码实现上同样需要，换掉被测对象即可保住价值。**本轮未执行**（需真实 ffmpeg + 大量造数）。

### 8. 保留项（刻意不改）

- `persistence/strategies/hls_strategy.py`、`workers/hls_worker.py`、`workers/segment_sweeper.py`、
  `services/traceback/segment_finder.py` 四个文件**一个字没动**；`PersistenceManager` 的
  `persist_hls_segment` / `flush_residual_segments` / `release_task_locks` / `start_run` 四个方法体也
  留着。只切接线点（决策③）。
- **不做回落层**：不加任何旧平铺布局的兼容分支（决策①）。
- `app/storage/hls/_write.py` 不改：**转码 / tfdt 失败 = 整段作废**，不回退到旧的「降级保留 mp4v
  照样进清单」（决策④）。

## 变更效果

### 行为变更清单

| # | 变更 | 影响面 |
|---|---|---|
| 1 | **读侧只认 `{step}/hls/`** | 平铺布局的老数据在回放 / 时间轴 / 大屏历史 / 送标清单 / 整段导出 / 离线反查上全部不可见。已定：不做回落，随 TTL 消失 |
| 2 | `/media/*` 对非法文件名 **404 → 400** | 唯一一处「更严」。`evil_init.mp4`、`../../etc/passwd`、裸 `init.mp4` 都在解析那步判死。正常 token 不受影响（清单签发的名字一定合法） |
| 3 | VOD `TARGETDURATION` `round` → `ceil` | traceback 侧：段长 ≤ 10.0 s 输出逐字节不变；> 10.0 s 时从违规的 10 变成合规的 11 |
| 4 | `clip_builder` 的 `TARGETDURATION` `int(max)+1` → `ceil(max)` | 段长恰为整秒时 11 → 10。RFC 8216 只要求 ≥ 每段 EXTINF，两者都合规 |
| 5 | `clip_builder` 清单补 `#EXT-X-MEDIA-SEQUENCE:0` | 现役这份历史上漏了（另两处有），换骨架后自动补齐 |
| 6 | `FrameTracker(task, step, "processed")` → `TypeError` | 从「能构造、后续读不到 sidecar」变成立刻报错 |
| 7 | `start_run` 的 eager rmtree 消失 | 整个 step 目录（含 `features/` / `lab/`）不再在 run 起始被删；改为本代次首写时只清 `{step}/hls/`。方向是好的：新 run 一段都没写出来时，旧录像原样保留 |
| 8 | 段写失败不再重试 3 次 | 记 error 丢这一段。重试会写出重复 EXTINF 毁掉整个 step 的回放 |
| 9 | TTL 判据换代 | 见下表 |
| 10 | timeline 的在途段过滤换解析器 | 口径不变（都以 playlist 键集合为准），从 `parse_playlist_durations` 换成写侧共用的 `_m3u8.durations` |

### 前后对照

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| HLS 落盘编排 | sweeper + `HLSPersistenceTask` + 2 worker pool + strategy | `submit_segment(cq, track, frames)` 一句 → 一条串行队列 |
| 并发控制 | `_dir_locks` 按目录抢锁 | 零锁（互斥是「根本没有第二个线程」） |
| 段写并行度 | 2（可配 1–4，配大了静默错） | 恒为 1，配置里没有这个旋钮 |
| 落盘位置 | `{task}/{step}/` 平铺 | `{task}/{step}/hls/` |
| VOD 清单构造 | 3 份手写（router / step_exporter / clip_builder） | 1 份骨架（`render_vod`）+ 各自的 URI 与时长来源 |
| 段名解析 | 2 份正则（`segment_finder` + `hls_strategy`） | 1 份（`hls.parse_segment_name`） |
| `SegmentFinder(...)` 构造样板 | 10 处 | 0（模块函数，不出句柄） |
| TTL 判据 | `metadata.json` 的 `updated_at` | `{step}` 目录自身 mtime |
| TTL 对新布局 | 匹配不到 → 永不回收 | 正常回收 |
| features-only 的 step | 永不回收（缺陷 #1） | 正常回收 |
| 活跃 step | 免疫（`updated_at` 每 ~10s 刷新） | **不再免疫**，见遗留风险 |

### 自测结果

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | **760 passed** |
| 新增 `tests/test_storage_cleanup_ttl.py` | 8 passed —— 两种布局的过期 step 都删 / 两种布局的新鲜 step 都留 / 写段不续命 / `lab/` 延迟创建续命 / `.lab_exports` 不进扫描 / 根不存在不抛 |
| `tests/test_import_hygiene.py` | 23 passed，**导入预算未调** |
| 未改动仍绿 | `test_recording_service` / `test_persistence_sink` / `test_hls_segment_sweeper` / `test_hls_eff_fps` / `test_traceback_segment_finder` / `test_offline_pipeline`，以及 `test_frame_tracker_boundary` 里测 `Timeline` 的那几组（保留构造 + 保留旧代码是这条绿的前提） |
| dev 端到端启停 | **未做**（会写真实 DB / 起真实录制），见遗留风险 |
| `integration_tests/test_frame_tracker_roundtrip.py` | 只做静态改动，**未执行** |

新增用例：`/media/init` 对 `evil_init.mp4` / `init.mp4` / `_init.mp4` 返回 400（token 需绕过 `sign`
校验伪造，测试里有 `_forge_token`——签发与消费是两处独立把关点）；`/task/history` 与 `/lab-f3m8/tasks`
跳过「有 step 目录但两轨都没段」的 step；`FrameTracker` 的 ts 位级相等 / 段级裁剪只碰该碰的段 /
不回落平铺 / `track` 形参已不存在。

> `tests/test_task_live_history_api.py` 的 `_write_segments` 有一处必须注意：`mtime` 要**同时**打在
> step 目录与 `hls/` 子目录上——`tasks._latest_step_mtime` 取两者最大值（产物落在域子目录里，只 stat
> step 目录会退化成「首次落盘时刻」）。这不是测试的怪癖，是生产口径的直接后果。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **dev 端到端启停未验证** | 本轮同时换了编排 + 格式 + 并发模型，单测覆盖不到进程级起停顺序与真实 ffmpeg 转码 | 需人确认后在 dev 跑一次：启停 + 新 step 回放 + 送标 + 离线反查 |
| **活跃 step 不再免疫 TTL** | 一个连续跑满 `cleanup_days`（当前 15 天）的 step 会被删掉自己正在写的录像 | 当前任务超时 30 分钟、触发不到。**把 `cleanup_days` 调小或引入长跑任务前必须先处理**（最小方案：删除前排除 `client_manager` 里活跃的 `(task, step)`） |
| **老数据在 TTL 到期前不可回放** | 升级后既有录像立刻从各清单消失，但盘上还占着空间直到 15 天后 | 已定的决策②。急需回看某个老 step 时，可手工把 `{step}/*.mp4|.idx|.m3u8|metadata.json` 挪进 `{step}/hls/`（同卷 rename、瞬时、幂等） |
| **旧 HLS 四件套仍在仓库里** | 代码存在但不启动；误启用 = 两个 sweeper 争抢 CQ、产出互相缺帧的段，两端都不报错 | 靠 `PersistenceManager.start()` 的 docstring + 本记录守。删除是 MAP §5.1 阶段 6 的事 |
| **`clip_builder` 仍不滤在途段**（缺陷 #3） | 原失败模式已消失，见右 | **不再会截短，但原因不是「窗口消失」**：旧写侧先落 mp4v 再转码，段名一出现就可见，clip 捡到 mp4v 喂 HLS demuxer 解不开 → ffmpeg 停在那里 → 截短。新写侧在 `.stage_*/` 里编码转码完再 `os.replace`，**段文件一出现就是完整 fMP4**。窗口本身还在（`os.replace` 与 `_m3u8.append` 之间；`append` 抛 `OSError` 时还会永久留下「有文件没条目」的段），但那种段**能解码**，而 `clip_builder` 自算 EXTINF、根本不读清单，捡到它只是多一段、不是坏数据。形式上的修复归第 5 期 |
| `persistence_config.yaml` 的 `hls:` 段已失效 | 配置项还在、改它不再有任何作用 | 随阶段 6 删旧代码时一并删 |
| 空 task 目录回收有两份实现 | `cleanup_worker` 的扫尾循环与 `tasks.delete_step` 内的 `rmdir` 各一份 | 接受（理由见 §4）。`cleanup_worker` 归属未定（MAP §6.3 决策点 4），并入 `retention` 时一起收口 |
| `/media/*` 的 400 对前端是静默变更 | 老前端若对 404 有重试/降级、对 400 没有，表现是「不重试了直接报错」 | 正常签发的 token 走不到这条路径，风险只在**过期链接被翻出来重放**。前端若有「playlist 缓存 + 段 URL 长期持有」的用法，上线前同步一句 |
