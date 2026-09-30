# hls 域读侧定型：只出段容器与 Frame 两种产出，VOD 渲染落新建的服务层工具层

> **变更状态**：生效中（2026-09-12 起，2026-09-13 契约收敛后重写本篇）
> <!-- 同一开发任务两天的改动合成一篇；中途推翻过一次界线，见「方案选型」。文件名保留起始日期，三处外链不动 -->
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[DESIGN_HLS_TIMELINE.md](../kb/DESIGN_HLS_TIMELINE.md)（2026-09-20）
>
> **本篇不复述代码里的东西**：每个函数的签名语义、实现细节的账（`bisect` vs
> `searchsorted`、`'right'-1`、`hi=-1` 不 clamp 之类）都在对应模块的 docstring 里，
> 抄进来只会变成两份各自漂移的同一知识。这里只写 docstring 写不了的：为什么改、
> 推翻了哪条既有决定、跨文件的并存状态、验证与遗留风险。
>
> <!-- 第 4 期（迁调用点）拆成 7 个可独立上线的阶段后的**阶段 0** -->

## 概述

给 `app/storage/hls/` 补齐读侧能力并**把读侧契约定死成两种产出**：

```text
① 段容器   片段元数据的查询与定位 —— 有哪些段、在哪、能不能播、多长、哪些落在某区间
② Frame    把段还原为帧

第三种产出要进来先问：它是段的元数据，还是给别人消费的装配产物？
VOD 清单是后者 → 不在域内，落 app/services/utils/vod_playlist.py（本次新建的分层）
```

**不迁任何调用点**：三处现役 VOD 构造、`segment_finder.parse_playlist_durations`、
`offline/frame_tracker.Timeline` 一个字未改，运行时行为零变化。

## 变更背景

### 现状 / 痛点

storage 第 4 期要把读侧调用点迁进域，但域里**缺五样它们都要的能力**，不补齐就无处可迁：

| 编号 | 缺口 | 现状 | 风险 |
|------|------|------|------|
| #1 | VOD m3u8 构造 | **3 份各写一遍**：[routers/traceback.py:160](../../app/routers/traceback.py)、[lab/step_exporter.py:180](../../app/services/lab/step_exporter.py)、[lab/clip_builder.py:331](../../app/services/lab/clip_builder.py) | 已经漂移：`TARGETDURATION` 三种算法（`round` / `ceil` / `int+1`），clip 还漏了 `MEDIA-SEQUENCE`。`round` 那份是**潜在 spec 违规**——RFC 8216 要求它 ≥ 每段 EXTINF，段长 10.4 s 会写出 `10` |
| #2 | 段级 EXTINF 读取 | 1 份实现 2 个消费方，住在读侧包 [segment_finder.py:273](../../app/services/traceback/segment_finder.py) | 写侧 `_m3u8` 有累计求和却没有逐段读取，正反运算不同居 |
| #3 | init 文件名**反向解析** | **不存在**，[routers/media.py:88](../../app/routers/media.py) 用 `endswith("init.mp4")` 顶替（放行 `evil_init.mp4` / `../raw_init.mp4`） | L2 落实不了：路径只能拼完再事后校验 `resolve()` 在不在根里，而不是由结构重建。**不是在跑的漏洞**（token 自签 + `.exists()` 兜底），但留着它阶段 2 就删不掉 `media.py::_resolve_media_path` |
| #4 | **双轨一次枚举** | `SegmentFinder._scan_step_dir` 有，域内只有单轨的 `list_segments` | 照现状迁，[routers/task.py:254](../../app/routers/task.py) 要对双轨各调一次——**一次 `iterdir` 变两次**，而大屏历史那个端点一次请求最多深扫 `_HISTORY_SCAN_CAP=30` 个 task |
| #5 | **段级区间定位** | 域外 2 套实现；域内那份埋在 `iter_frames` 里，**不出签名** | `Timeline.iter` 迁不过来——域里没有能替代它段级裁剪那一段的公开函数 |

### 触发来源

第 4 期原定「迁调用点 + 删 `segment_finder` + 改名 + 门禁」一次做完，是 breaking 变更。
经开发者拍板改为**不清空 `database/`、加临时读侧路径回落**，第 4 期随之拆成 7 个可独立
上线的阶段。本篇是**阶段 0**：纯增量补能力，为阶段 2（读侧调用点迁移）铺路。

与本篇直接相关的只有前三个阶段，列在这里免得读者去找分期表：

```text
阶段 0（本篇）  给域补齐读侧能力，不迁任何调用点
阶段 1          加临时读侧路径回落层，让域内读函数也认 {step}/ 平铺布局
阶段 2          迁读侧调用点（traceback / lab / routers），价值兑现点
```

### 承接

- 落盘结构、域根、`SegmentRef` 来自 [hls 域落地](20260911_STORAGE_HLS_DOMAIN.md)。
- EXTINF 语义（段时长唯一真值、键集合即"已登记"）取自 [EXTINF/tfdt 契约](20260908_EXTINF_TFDT_CONTRACT.md)。
- 设计规范见 [DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)（读侧 R1-R6 在 §4）；本篇修订了 R4 的例子，见 §4。规范原先住在 [storage 分层](20260909_STORAGE_LAYER_BASE.md) §7，已于 2026-09-13 独立成篇。
- 分层与 `__init__` 纪律见 [包结构规范](20260903_PACKAGE_LAYOUT_SPEC.md)；本篇在它之上加了一层，见 §2。

## 方案详情

### 全景：域出事实，装配归服务层

```text
app/storage/hls/                          ← 数据层：只回答落盘事实
  ① 段容器  list_segments / list_segments_by_track / select_segments / playable_segments
  ② Frame   read_segment / iter_frames
        │ [PlayableSegment(ref, duration_s), ...]
        ▼
app/services/utils/vod_playlist.py        ← 服务层工具：无状态纯函数
        │ 调用方决定每条 URI 与每段时长从哪来
        │   token 化 URL + 清单 EXTINF    routers/traceback → 浏览器 hls.js
        │   裸文件名     + 清单 EXTINF    lab/step_exporter → ffmpeg 整段导出
        │   裸文件名     + 相邻段 ts 差   lab/clip_builder  → ffmpeg 裁 clip
        ▼
render_vod(entries, *, map_uri) ──▶ m3u8 文本（含 ENDLIST、TARGETDURATION = ceil(max)）
```

落点与各自的理由**写在代码里**，本表只给索引：

| 能力 | 落点 | 理由在哪 |
|------|------|---------|
| 服务层工具层（新分层） | `app/services/utils/__init__.py` | 该包 docstring 的边界表；跨层取舍见 §2 |
| VOD 条目形状 + 文本渲染 | `services/utils/vod_playlist.py` | 该模块 docstring（含四条硬格式约束） |
| 段级区间定位 | `_read.select_segments` | 该函数 docstring（判据 / `bisect` 等价性 / 边界语义） |
| 逐段 EXTINF 解析 | `_m3u8.durations`（**包内私有**） | 该函数 docstring（返回值双职责、为何不出 facade） |
| 可播段过滤 | `_read.playable_segments` | `_read.py` 模块 docstring（两种"文件在但不能播"） |
| 双轨一次枚举 | `_layout.list_segments_by_track` | 该函数 docstring；不出 step 摘要类型的理由见 §5 |
| init 名反向解析 | `_layout.parse_init_name` | 该函数 docstring（L2 的执行形态） |
| 对外 facade（+7 个名字） | `hls/__init__.py` | 该文件 docstring 的「读侧只出两种产出」一节 |
| 资源容器 | `hls/types.py`（新建，子包的底） | 该模块 docstring；不进 `app/domain/` 的判据见 §5 |

### 方案选型

界线画在哪，本任务中途改过一次——**这是本篇唯一被推翻的决定**，两版都记在这里。

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **界线画在「产物 vs 元数据」：VOD 整体出域（采用）** | 多一个分层 + 一条门禁 | **采用**。域只出段的元数据与帧 |
| 界线画在 URI 上：域出 `render_vod` + `VodEntry`，只让 URI 归调用方 | 无新增分层 | **否（曾实现，次日移出）**。留域里那个函数只差一个字段就变成「供人塞 token 的钩子」，而域对 token 本该零认知 |
| 域出 `VodPlaylist` + `.with_uris(fn)` 批量覆写 | 多一个类型 + 一个钩子 | **否（曾实现后删除）**。`with_uris` 没有一行域知识，就是个字段 `map` |
| `build_vod_playlist` 出裸文件名结构 | 省调用方一次映射 | **否（同上删除）**。映射必须在签名那一侧做；留着就有两条构造路径 |
| VOD 工具放 `app/utils/` | 无新增包 | 否。那边是**全层通用基建**（异常层次 / `GuardedExecutor` / 指标 / `SerialTaskQueue` / 网关），与业务格式无关。塞一个 m3u8 渲染器进去，下一个人塞什么就没判据了 |
| VOD 工具放 `app/services/media/`（service 包） | 无新增分层概念 | 否。那会长成一个被别的 service 引用的 **service 包**，撞上「不建 service 对 service 的直接依赖」；且三个消费方有两个在 `lab`，让 lab 反向 import 回放包也读不通 |
| `render_vod` 改收 `PlayableSegment` + `uri_of` 回调 | 三个调用点各省一行 | 否，见 §1 |
| token 签发进域 | 域要认识 `MediaToken` / base_url / 路由形状 | 否。包 docstring 边界清单明令「MediaToken / HTTP 状态码 / token 化 URL → routers」 |
| `list_segments(..., playable_only=)` 必填 bool | 符合 R4 原文，漏传是 `TypeError` | 否（经确认），见 §4 |
| 连读侧调用点一起迁 | 本期 diff 翻倍，且要同时处理 `{step}/` 平铺兼容 | 否。阶段 0 的价值就是让阶段 2 是一次纯替换 |

**「产物 vs 元数据」这条判据怎么用**——问一句「把落盘方式整个换掉，它还在吗」：

```text
SegmentRef / PlayableSegment    它们就是盘上那个文件名与那份清单解出来的  → 跟着消失 → 在域内
Frame                           换成 S3、换成数据库，帧还是帧             → 还在   → app.domain
VOD 清单                        "一份 VOD 由若干 (URI, 时长) 组成"照旧成立 → 还在   → 不在域内
```

第三行方向与前两行**相反**，这就是它不属于本域的证据。旧界线当时的理由是「m3u8 是一种容器
格式的形状，与落盘格式同生共死」——那句话对 `{track}_playlist.m3u8`（本域的落盘产物）成立，
对**现生成、不落盘的 VOD 文本**不成立，是把两者混为一谈了。

### 1. 三处 VOD 里有一处的时长口径不同 —— 这决定了后面两个签名

这条是跨三个文件的事实，没有单个 docstring 是它的主人，故写在这里：

| 调用点 | URI 形态 | **每段时长从哪来** |
|---|---|---|
| `routers/traceback.py` | token 化 HTTP URL | 清单 EXTINF |
| `lab/step_exporter.py` | 裸文件名 | 清单 EXTINF |
| `lab/clip_builder.py` | 裸文件名 | **相邻段 ts 差（实测墙钟）** |

`clip_builder` 那份是**有意的**，理由在 [clip_builder.py:317-320](../../app/services/lab/clip_builder.py)：
它的 `-ss` seek 基准是墙钟 ts，而 EXTINF 是 `N/fps` 推出的恒定 10.000 s；真实采集达不到
`raw_fps` 时相邻段墙钟间隔会系统性 > 10 s，用 EXTINF 会让累计时长偏离墙钟、逐段 seek 错位。

**能被统一的只有文本骨架，不是时长来源。** 这条直接否掉了「工具函数收 `PlayableSegment`」
（那等于把「时长必须来自清单 EXTINF」钉死在签名上，而 `clip_builder` 正是三个消费方之一），
也说明了为什么工具不该住在域里——域给不出「时长你自己定」这种契约而不自相矛盾。
顺带地，收 `VodEntry` 让 `vod_playlist.py` 保持 **stdlib only、不依赖 `app.storage`**。

### 2. 新建 `app/services/utils/` —— 服务层工具层

VOD 渲染出域后要有地方住。`app/utils/` 与 `app/services/<svc>/` 都不合适（选型表末两行），
于是在两者之间补一层：

```text
app/
  domain/          内存数据契约（Frame / FrameFeature）
  storage/         数据层：内存模型 ↔ 盘上字节（leaf）
  utils/           基建：异常 / 执行器 / 指标 / SerialTaskQueue / 网关（与 storage 平行的 leaf）
  services/utils/  ← 本次新建。服务层工具：无状态纯函数，不持活体、不是服务
  services/<svc>/  业务服务：活体单例 + lifespan + 编排与策略
  routers/         装配层：HTTP 协议、token 签发、URI 拼装
```

完整边界声明（可 import 什么、不许有什么、准入判据）在
[`app/services/utils/__init__.py`](../../app/services/utils/__init__.py) 的包 docstring。
只有一条必须在这里再说一遍，因为它是这一层**存在的前提**：

> **不许 import 任何兄弟 service 包。** 破了它，本包就是 service → service 依赖的后门：
> `lab` 想调 `traceback` 的东西，在这里加个转发函数就绕过去了，而
> `test_singleton_reference_surface` 只盯单例、看不见这种转发。

这条由门禁守，见 §3。

### 3. 门禁：把新分层写成可执行约束

`tests/test_import_hygiene.py` 三处改动（细节与理由在该文件的注释里）：

- **`LAYER_PACKAGES` 加 `app/services/utils`** + 白名单。注意 `app.services.utils` 作为前缀
  **不会**放行 `app.services.lab`——检查是 `name == ok or name.startswith(ok + ".")`。
- **`BUDGET` 加四行**：`app.services.utils` / `.vod_playlist` / `app.storage.hls._read` /
  `app.storage.hls.types`（都 stdlib-only、0.20 s）。`LAYER_PACKAGES` 参数化着「每个模块
  都得有预算」那条，加包就必须逐模块登记。
- `test_storage_modules_are_all_budgeted` → **改名** `test_layer_package_modules_are_all_budgeted`
  （它早已对 `LAYER_PACKAGES` 参数化，名字里的 storage 成了误导）。

**门禁验证过会红**：往 `vod_playlist.py` 里加一行 `from app.services.lab.step_exporter
import StepExporter`，该条当场失败并点名具体行号，随后还原。没做这一步就不知道门禁是不是
形同虚设。

### 4. 对规范 R4 的修订：例子换成两个函数

R4 原文举的例子是 `segments(..., playable_only=)` **必填 bool**（防缺陷 #3 那类漏滤）。
本期经开发者确认改为**两个函数**：`list_segments`（盘上有哪些段文件）与
`playable_segments`（已登记、带 EXTINF）。

| | 必填 bool | 两个函数（采用） |
|---|---|---|
| 漏滤防护 | `TypeError`，结构性 | docstring + review |
| 拿 EXTINF | 还要再调一次 `durations`，**扫两次清单** | 一次调用一次扫盘（R5） |
| `_decode` / `select_segments` / 往返测试 | 被迫传一个与它们无关的 bool | 用 `list_segments`，签名干净 |

**R4 的条文本身不变**（正确性参数必填、无默认仍然成立），变的只是这个例子；
落点已随规范独立成篇迁走，现在写在 [DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)
§4 的「R4 与 R5 打架时，切函数优先」一段（R4 行本身的例子换成了 `ids(order=)` 与
`read_segment` 的 `width`/`height`）。

### 5. 评估过但**没建**的三个类型

这三个代码里没有痕迹，只能记在这里：

- **`Step(step_id, tracks, first_ts_us, last_ts_us)` 摘要**（曾实现后撤）。两条理由：①
  **同名不同义**——`step` 在本仓库已是业务实体（`clean_task.current_step`、
  `clean_alarm.step_id/step_name`），且 step 是跨域的（`features/` 与 `lab/` 也住同一个
  step 下），这名字哪个域都不该独占；② **准入判据 2「< 2 不进」不成立**——要完整摘要的只有
  `routers/task.py` 一个消费方（`routers/lab.py:170` 只问"有没有 raw 段"）。于是"两轨都没段
  就丢弃""跨度取双轨并集"留在调用方，`TestStepSummaryRecipe` 把那段配方与现役
  `SegmentFinder.list_steps` 钉成逐字段相等。
- **`TaskRef`**——两个 listing 接口的消费方（`lab.py:234`、`task.py:247`）**都只拿 task_id
  去循环**，`TaskRef` 里唯一能放的就是调用方已经拿着的那个 key（L3 挡的正是这个）。把
  `recency` 那个 mtime 近似值放进去更糟：它现在刻意只当排序键、不出口，进了结构体就会被当
  时间戳渲染。
- `feature.py` / `tasks.py` 的 **`types.py`**——前者货币是 `app.domain` 的 `FrameFeature`，
  后者只出 `List[int]` / `bool`，都没有自己的形状。空文件不建。

**结论不是"storage 的类型都别建"**，而是每个类型都要自己过准入判据 2。留下的两个各有 ≥2
个消费方：`SegmentRef` 贯穿读写两侧，`PlayableSegment` 服务 traceback 与 step_exporter。
「不进 `app/domain/`」的判据就是选型表那三行——调用方 import 本层不带任何重依赖，资源容器
就地声明即可。

### 6. 保留项（刻意不改）

- **三处 VOD 构造原样保留**，仍是现役且读 `{step}/` 平铺布局；**`Timeline` 不动**
  （`select_segments` 有了但替换归阶段 2）；**`segment_finder.parse_playlist_durations` 不删**。
- **`media.py` 不动**：`_resolve_media_path` 与 `endswith` 判据照旧，`parse_init_name` 暂无
  生产消费方。
- **token 形态不动**：payload 仍是 `{t, s, f, k, e}`。改它等于让所有已签发 token 失效，且
  [docs/api/media.md](../api/media.md) 的对外契约要跟着改。
- **不把 clip 的 ts 跨度时长口径与窗口选择收进域**：前者有意（§1），后者是业务判断。

## 变更效果

| 维度 | 变更前 | 变更后（阶段 2 接上才生效） |
|------|--------|------------------------------|
| hls 域读侧契约 | 没有成文契约，"域该不该出 X"每次现讨论 | 两种产出写死在包 docstring；第三种要进先过「产物 vs 元数据」 |
| VOD 清单构造 | 3 份各写一遍，~40 行重复文本拼装 | 骨架只剩一份，且**不在数据层里**；每处只留自己那行 URI 映射 |
| `TARGETDURATION` / `MEDIA-SEQUENCE` | `round`/`ceil`/`int+1` 三种；clip 缺 MEDIA-SEQUENCE | 统一 `ceil` 下限 1（修 RFC 8216 违规）；MEDIA-SEQUENCE 恒有 |
| 在途段过滤 | 调用方各自记得滤（clip 忘了 = 缺陷 #3） | `playable_segments` 一次扫盘顺带给出，判据在域内 |
| 段级区间定位 | 域外 2 套，域内那份埋在 `iter_frames` 里不出签名 | `hls.select_segments` 公开，`iter_frames` 改调它 |
| 双轨枚举 / 段级 EXTINF | 住在业务服务包 `segment_finder` 里，与写侧求和分家 | `list_segments_by_track` / `_m3u8.durations`，与写侧共用解析器 |
| init 名校验 | `endswith("init.mp4")`（放行 `evil_init.mp4`） | `parse_init_name` 正则，路径由结构重建 |
| 服务层共用纯函数 | 无处可放，只能塞进某个 service 包或抄三遍 | `app/services/utils/`，依赖方向由门禁守 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_storage_hls.py` | **152 passed**（基线 104）。新增 `parse_init_name` 13 / `durations` 4 / `playable_segments` 6 / `list_segments_by_track` 3 / step 摘要配方 2 / `TestSelectSegments` **18**（含与 `np.searchsorted` 比对的 8 参数化） |
| **新建** `tests/test_utils_vod_playlist.py` | **12 passed**。不依赖 `fake_pipeline`：段与清单用域内写侧真函数 `_m3u8.append` 铺，不拖 cv2/ffmpeg |
| **与现役实现比对 ×2** | ✅ VOD：`render_vod(调用方映射的 entries, ...)` 与 `StepExporter._build_vod_text(...)` **逐字节相等**（三处 VOD 里唯一可直接调用的）。✅ step 摘要：同一组段名铺到平铺与 `{step}/hls/` 两处，调用方侧三行统计与 `SegmentFinder.list_steps` **四字段逐一相等**。两条合起来 = 阶段 2 迁 `step_exporter` / `routers/task.py` / `routers/lab.py` 都是零行为变更 |
| `tests/test_import_hygiene.py` | **30 passed**（基线 26）；`app/services/utils` 的白名单条**验证过会红** |
| 全量 `pytest tests/` | **738 passed**，零 failed（基线 671）。既有用例除移文件外一条未改 |
| `import app.storage.hls` 重依赖 | 仍只有 numpy（`HEAVY` 三项为空）；`_read` 没引进 numpy |
| 运行时行为 | 零变化——`grep -rn "render_vod\|VodEntry" app/` 只命中新模块本身与两处 docstring 指路 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **本期能力无生产消费方** | 新增的对外名字只有单测在跑（`select_segments` 多一个域内消费方 `iter_frames`） | 阶段的自觉代价，与第 1/2/3 期同一立场。阶段 2 是价值兑现点 |
| **VOD 构造四份并存** | 新的一份 + 现役三份，改任一份不会让别的红 | 阶段 2 一并收口。在那之前改任一处都要想到另外三处 |
| **`TARGETDURATION` 从 `round` 改 `ceil` 是行为变更** | 阶段 2 迁 `traceback` 那一刻生效：段长非整秒时该值 +1 | 有意选择（修 spec 违规）。播放器只拿它预留缓冲，偏大无害、偏小才违规 |
| **`clip_builder` 的段尾口径不会被统一** | 它的区间重叠判据依赖估算段尾，与 `select_segments` 的段起始判据不同 | 有意留下（§1）。缺陷 #3 归第 5 期 |
| **R4 的漏滤防护退化成 review** | 有人直接用 `list_segments` / `select_segments` 拼清单不会红 | 已在两个函数的 docstring 写明并指路；门禁抓不到 |
| **`app/services/utils/` 可能长成杂物间** | 「服务层共用」这名字比 `hls` 之类的域名宽，挡膨胀能力弱 | 包 docstring 写死准入判据「< 2 不进」+ 边界表；门禁只挡依赖方向，挡不住往里塞东西 |
| **`durations` 不认带标题的 EXTINF** | `#EXTINF:10.0,title` 会被当坏行跳过 | 本域写出的条目从不带标题（两代写侧均已核对），只读自己的清单 |
| **域内读函数只认 `{step}/hls/`** | 现有平铺数据一个字节都读不到 | 阶段 1 的临时读侧路径回落层解决（见「触发来源」的阶段表），那是阶段 2 的前置 |
| **KB 漂移：`SegmentFinder.find` 不存在** | [SERVICE_TRACEBACK_MEDIA.md:26](../kb/SERVICE_TRACEBACK_MEDIA.md) 写了它，读 KB 的人会去找一个没有的函数（代码里只有 `list_segments` / `list_steps` / `list_task_ids*`） | 记在此处，留下次 kb-merge 修 |
