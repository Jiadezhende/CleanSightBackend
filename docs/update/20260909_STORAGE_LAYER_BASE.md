# 抽 storage leaf 包：基础能力 + 产物按域隔离目录，零调用点改动

> **变更状态**：生效中（2026-09-09）　<!-- 新包已落地并有单测/门禁覆盖；本期刻意不接调用点，包内代码暂无生产消费方。落盘目录结构的实际变更发生在第 4 期迁移那一刻 -->
> **知识库**：待沉淀
>
> **追加（2026-09-09）**：包已迁址 `app/services/storage/` → **`app/storage/`**，门禁从
> 黑名单换成白名单式，见 §9；`_locks.py` 落地见 §8；**§7 规范已换判据重写为定稿**
> （storage 从「名字与定位」变成数据层，推翻记录在 §7.0）。
> 正文 §1–§5 与「变更效果」里的路径是迁址前的原文，**未回改**——它们记录当时做了什么。
>
> <!-- 本篇是分期改造的第 1 期，后续期次见文末「遗留风险 / 后续任务」。 -->

## 概述

新建 `app/services/storage/` 作为落盘布局的零跨服务依赖 leaf 包，本期只落两个模块：
`_root.py`（域名白名单 + 逐级定位，包内私有）与 `tasks.py`（跨域的枚举与删除）。同时把
落盘结构从「step 目录平铺」改为**按域隔离**（`{step}/hls/`、`{step}/features/`、
`{step}/lab/`）。**不迁移任何调用点**，故运行时行为零变化，目录结构的实际切换发生在第 4 期。
附带删掉 `app/services/__init__.py` 里零消费方的 `client_manager` re-export——它让每个
`app.services.*` 导入白付 282ms。

## 变更背景

### 现状 / 痛点

`{storage_base_dir}/{task_id}/{step_id}/` 的落盘格式知识散在 4 个服务包 + 4 个 router，
实测 11 类重复：

| 重复项 | 份数 | 位置 |
|--------|------|------|
| 段文件名正则 | 2 | [segment_finder.py:20](../../app/services/traceback/segment_finder.py)、[hls_strategy.py:135](../../app/services/persistence/strategies/hls_strategy.py) |
| EXTINF 解析（手法还不同） | 2 | `segment_finder.py:273` 出 dict；`hls_strategy.py:143` 自己 re + 求前缀和 |
| VOD m3u8 构造 | 3 | [traceback.py:107](../../app/routers/traceback.py)、[step_exporter.py:169](../../app/services/lab/step_exporter.py)、[clip_builder.py:331](../../app/services/lab/clip_builder.py) |
| `{track}_init.mp4` 存在性判据 | 3 | 同上三处，注释已自认是复制 |
| 在途段过滤 | 2（应为 3） | traceback、step_exporter 有；**clip_builder 没有** |
| `SegmentFinder(get_default_base_dir())` 构造样板 | 10 | routers ×6、clip_builder、step_exporter、frame_tracker |
| `{base}/{task}/{step}` 目录拼装 | 4 | hls_strategy ×2、[feature/store.py:137](../../app/services/inference/feature/store.py)、segment_finder |

**这不是谁偷懒，是布局逼出来的**：`app/services/traceback/` 是业务语义包，却被
`inference.offline` 和 `lab` 依赖，只为拿一个落盘解析器；而写侧 `persistence` 不敢反向
依赖读侧，只好把正则和 EXTINF 解析再写一遍。

由此长出三条真缺陷（本期均**不修**，此处记录动机）：

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | [cleanup_worker.py:71](../../app/services/persistence/workers/cleanup_worker.py) 用 `glob("*/*/metadata.json")` 判 TTL，而 `metadata.json` 只有 `hls_strategy` 写 | 有 `features.jsonl` 无 HLS 段的 step 目录**永不回收**，确定性泄漏，正在漏 |
| #2 | [hls_strategy.py:112](../../app/services/persistence/strategies/hls_strategy.py) `purge_step_dir` 自述"删 HLS 产物"，实际 `rmtree` 整个目录，连带删 inference 的 `features.jsonl`/`facts.jsonl`；它与 `FeatureStore.open_fresh` 的先后约束**全部载体是 [run_control.py:90-92](../../app/services/run_control.py) 的三行注释** | 顺序一动即静默丢数据，两侧代码互不知情 |
| #3 | `clip_builder._select_segments` 不滤在途段 | clip 静默截短，`ClipResult` 报的是请求时长，送标无感 |

### 触发来源

架构评审：从「资源读写查询能力应不应该抽出来」起，逐层追问到「一个领域的资源抽一个工具
文件」「能用参数解决的不拆方法」「能力尽量朴素、不混业务逻辑」三条约束，据此定下包结构。

### 承接

本次建立在 [20260903 包结构与导入纪律规范](20260903_PACKAGE_LAYOUT_SPEC.md) 之上——该规范
定下 `__init__.py` 零副作用、依赖分级 L0-L3、导入门禁三件事，本包的「stdlib-only leaf 且
settings 只在函数体内 import」正是它的直接应用。另外
[fade30b](../../app/routers/traceback.py) 下线告警证据反查之后，`services/traceback/` 里
已无任何"取证"逻辑（只剩 `media_token.py` 鉴权 + `segment_finder.py` 格式），
`segment_finder` 留在那里的最后一个理由也没了。

## 方案详情

### 全景：目标落盘结构 + 分 6 期

产物按域隔离，**step 根下只有域目录、没有文件**；存储根下只有数字命名的 task 目录：

```text
{root}/{task_id}/{step_id}/
  hls/       {track}_segment_{ts_us}.mp4  {track}_init.mp4
             {track}_playlist.m3u8  raw_segment_{ts_us}.idx  metadata.json
  features/  features.jsonl  facts.jsonl
  lab/       clip_*.mp4  step_*.mp4   送标/导出临时件，用完即删
```

对照改造前——所有产物平铺在 step 根，且存储根下还寄居着 `.lab_exports/` 与
`lab_runtime_config.json` 两个非 task 目录。

```text
1  本篇  建 _root.py（domain 隔离）+ tasks.py + 单测 + leaf 门禁    零调用点改动
2  ✅    建 hls/ 子包（insert_segment 写侧）+ 单测                零调用点改动
3  ✅    建 feature.py + lab.py + 单测                           零调用点改动
4        迁移调用点 + 删 segment_finder.py
         + traceback 改名 media_access + 门禁②③                 **落盘结构在此切换**
5        修 #3 clip_builder 在途段                               行为变更，独立评审
6        修 #1 TTL 判据换代 / metadata.json 存废                  格式变更，独立立项
```

> **实际顺序：3 先于 2 落地。** `feature.py` 已于同日建成，见
> [20260909_STORAGE_FEATURE_DOMAIN](20260909_STORAGE_FEATURE_DOMAIN.md)（含 §7 规范的
> 首轮回填：补写侧路线 C、读侧 R6）；第 2 期 `hls/` 子包已于 2026-09-11 落地**写侧**
> （`insert_segment`，读侧枚举与 VOD 清单随第 4 期迁），见
> [20260911_STORAGE_HLS_DOMAIN](20260911_STORAGE_HLS_DOMAIN.md)。两个域文件无依赖关系
> （各自绑死域名、各自经 `_root.path`），换序无技术代价。第 3 期只做了 `feature.py`，
> `lab.py` 仍未开工。
>
> ⚠ **§7.4 的并发条文已被 hls 域推翻**：本层不再自己持锁（`_locks.py` 已删），顺序改由
> `app/utils/task_queue.py` 的 `SerialTaskQueue` 按提交序构造，理由见 HLS_DOMAIN §4。
> C1–C7 里凡是说「锁归层内」的，一律以那一节为准。

**本期产出无生产消费方**（仅新单测覆盖）。这是分期的自觉代价，换来第 4 期的迁移可以是
一次纯替换、diff 全是「删一行加一行」，而不是「抽层 + 改调用 + 改行为」搅在一个 PR 里。

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 包边界声明（落盘结构 + 不属于本包的清单） | `app/services/storage/__init__.py` | §1 |
| 根解析与逐级定位（可选建目录） | `app/services/storage/_root.py` | §2 |
| 域隔离（`domain` 白名单 + 必填） | `app/services/storage/_root.py` | §2 |
| 跨域的枚举 / 删除 | `app/services/storage/tasks.py` | §3 |
| leaf 身份门禁 + 导入预算 | `tests/test_import_hygiene.py` | §4 |
| 顺带：删死 re-export | `app/services/__init__.py` | §5 |
| 各域文件的读写准入规范（**约束第 2/3 期**，非第 1 期产出） | 条文见 §7，摘要进 `__init__.py` docstring | §7 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **薄层：只搬「名字与定位」（采用）** | 本期新包 421 行（`__init__` 80 / `_root` 160 / `tasks` 181，含大段契约注释）；调用点迁移推到第 4 期 | 采用。三条缺陷全出在「路径 / 命名 / 删除 / 产物可见性」这一薄公共面上，没有一条出在「内容怎么写」上 |
| 厚层 Repository / DAO | 要搬走批缓冲、目录锁、owner fence、ffmpeg 转码 | 否。那不是「抽一层」，是重写 persistence 与 inference/feature；且包会吃 L2 依赖，leaf 身份立刻破 |
| 只去重不建包 | 写侧仍不敢依赖读侧 | 否。重复正是当前布局逼出来的，不动布局就会重新长出来 |
| 抽层 + 迁移 + 修缺陷一次做完 | 单个 PR 巨大，回归风险与评审成本都不可分割 | 否。无法分次上线、无法只回滚一半 |

**对外按域分文件，不按读写分**：`read.py`/`write.py` 会让「盘上有哪些 step」（目录层）
和「这个 step 的段有哪些」（HLS 域）挤在同一个模块里，两个抽象层级。按域切之后每个函数
自带域限定词——`hls.*` 不可能被要求回答 `features.jsonl` 在哪，名字本身就在挡膨胀。

### 1. `app/services/storage/__init__.py` — 标记型，只写边界（对应全景第 1 步）

**包名叫 `storage` 不自带约束**（「存储」听起来什么存储相关的事都能进），故 docstring 里
逐条列出**不属于本包**的东西，作为唯一的边界声明：

```text
ffmpeg 转码参数 / tfdt hex-patch / timescale pin   persistence/hls_strategy 独家
cv2.VideoWriter / eff_fps 反推                     同上（三者是一个原子正确性约束）
段解码起 ffmpeg 子进程                             inference/offline
FeatureStore 批缓冲 / owner fence / open_fresh     inference/feature（有状态）
目录锁                                             persistence（并发机制不是格式）
TTL 保留天数 / 扫描周期 / 删不删                    persistence/workers（策略）
MediaToken / HTTP 状态码 / token 化 URL            routers（鉴权与表示层）
异步调度（队列 / WorkerPool / strategy 分发）       persistence 的本职
```

> ⚠ **这份清单已被 §7.0 的判据换代推翻四行**（ffmpeg 转码参数 / cv2 + eff_fps / 目录锁 /
> 起子进程，全部移入本层），此处保留原文只为记录第 1 期当时是怎么划的。**现行边界以
> §7.0 与包 docstring 为准**，别照这段往外推。

清单里新增一条**Label Studio 运行时配置不归本包**：`lab_runtime_config.json` 是配置不是
产物，**没有 `(task_id, step_id)` 身份键**，它此前寄居存储根纯属搭便车。它降级为 `settings`
的一个路径字段（可 env 覆盖），由 `lab/config.py` 自己读——本包一概不认识它。

> 顺带排除了「放进 `config/`」这条路：`lab/config.py` 的 `update()` 由
> `POST /lab-f3m8/config` 触发，送标页面改完 URL / project_id / task_source **立即落盘**，
> 是运行时可写状态；而 `config/` 下五个 yaml 全是 git 跟踪的部署物料，
> [20260903 §8](20260903_PACKAGE_LAYOUT_SPEC.md) 明确它「放顶层才能在部署时整目录覆盖或
> 挂载」。放进去 = 部署一次冲掉用户设置，且只读挂载场景写不进去。

docstring 同处写下落盘结构（见全景）与 7 条设计约束（一域一模块 / 准入判据「几个包会因它
变了而出错，< 2 不进」/ 路径出包根不出包 / 不出句柄 / 能用参数不拆方法但正确性参数不给
默认 / 朴素不做业务判断 / 太窄的不包装）。规范 §3 的「标记型」形态：纯 docstring，不
re-export。

### 2. `app/services/storage/_root.py` — 包内各域文件的共同底座（第 1 步）

2 个成员 + 1 个常量。只做两件事：**定位**，以及**按需确保目录存在**。

```python
DOMAINS: Tuple[str, ...] = ("hls", "features", "lab")   # 封闭集合，唯一真源

def path(task_id=None, step_id=None, domain=None, *, create=False) -> Path
def dir_name_to_int(name: str) -> Optional[int]         # 非数字目录名 → None
```

`create` 放在这里而不是各域文件，是因为「定位」与「确保能往这写」是同一件事的两半、每个
写者都要做一遍——放下去就是三份重复的 `mkdir(parents=True, exist_ok=True)`。默认
`create=False` 保持不主动碰盘。枚举与删除仍不在这里（那是 `tasks` 域，见 §3）。

**`path()` 一个入口而不是 `root`/`task_dir`/`step_dir`/`domain_dir` 四个**：它们是同一条
路径的四个前缀，参数从左到右逐级下钻，省到哪一级就返回哪一级。用参数表达比用四个名字表达
更直白，也不会有人拼出第五种组合。跳级（给了深层却省了浅层）抛 `ValueError`——那会静默
少一层目录，产物落到上一级。

**`DOMAINS` 白名单**：域名属于「布局」归本包，产物文件名属于「内容」归各域自己。这条分界
决定了白名单该放这里。必须校验的理由是**静默失败**：域名笔误（`"feature"` / `"HLS"`）会
造出第四个子目录，写侧不报错、读侧只是"查不到"，而 `purge_step` 照样把它删掉——连残留
证据都不留。校验顺带挡住路径逃逸（`".."`）。新增一个域要改这个常量，这是有意的：往 step
目录里塞新子目录该是一次显式决策。

**包内私有**：交出去就等于交出「根 + 两级 id + 域」的拼装能力，settings 那道门禁就白设了
（拦属性访问拦不住 `from ..._root import path`）。

根解析（`_storage_root()`，模块内私有）的三条硬约束：

> - **必须记忆化**：`settings.storage_base_dir`（[settings.py:168](../../app/settings.py)）
>   每次访问都跑 `.resolve()`，那是文件系统 syscall；本包的路径函数会被每段落盘、每次
>   请求各调若干次。
> - **key 必须取 `settings.storage_dir` 原始字符串**，不能无条件缓存：
>   [tests/conftest.py:35](../../tests/conftest.py) 的 `tmp_storage` fixture 靠
>   `monkeypatch.setattr(settings, "storage_dir", ...)` 把读写两侧一起指到临时目录，
>   以原始值作 key 才能 patch 后自动重算。
> - **不能写成模块级常量**（`_ROOT = settings.storage_base_dir`）：那在 import 期定死，
>   上面那条 patch 完全失效，失效的表现是**测试去写真实 `database/`**。

相对路径→绝对路径的规则**不搬进来**，留在 `settings.storage_base_dir`（与
`settings.config_dir` 同款，以项目根为基）——那是路径解析通用规则，与"存储"这个域无关。

**枚举与删除刻意不放这里**：`steps()`/`ids()` 要 `iterdir`+`stat`、`purge_step()` 要
`rmtree`，都碰盘且都与「有哪些/删哪个」有关；混进来则各域文件 import 底座时会顺带吃一层
与自己无关的 IO 语义。

**`domain` 的两道执行机制各挡一种错法**，域隔离才是结构性保证而非约定：

| 机制 | 挡什么 | 不挡会怎样 |
|------|--------|-----------|
| **必填、无默认** | 漏传 | 文件写回 step 根，退回平铺状态——谁也说不清哪个文件归谁、`purge` 删掉了谁的东西。漏传现在是 `TypeError` |
| **`_root.DOMAINS` 白名单** | 打错 | `"feature"` / `"HLS"` 造出第四个子目录，写侧不报错、读侧只是"查不到"、`purge_step` 照样删掉，**连残留证据都不留**。现在是 `ValueError` |

前者是原则 6 后半句「涉及正确性的参数不给默认值」的第一个用例；后者是「会静默失败的知识
必须收进包」的直接应用。校验发生在 `mkdir` **之前**（`test_unknown_domain_creates_nothing`
钉死），否则错误的域目录已经落盘了才报错。

产物**文件名**则由各域自己持有，本包对其零知识、不校验——它是各域文件里的常量，不是外部
输入。域名是布局、文件名是内容，这条分界决定了白名单在包里、文件名在包外。

#### 各域文件的用法：先声明一个绑死自己域名的私有 root

约定写死在 `_root.py` 的模块 docstring 里，供第 2/3 期的 `hls.py` / `feature.py` /
`lab.py` 照抄：

```python
_DOMAIN = "hls"

# 本域在该 step 下的根目录 —— 域内所有路径函数都经它
def _domain_root(task_id, step_id, *, create=False) -> Path:
    return _root.path(task_id, step_id, _DOMAIN, create=create)

def segment_path(task_id, step_id, track, ts_us) -> Path:
    return _domain_root(task_id, step_id) / _segment_name(track, ts_us)
```

**域名在一个域文件里只出现一次**（写错一眼可见，且白名单当场拦下），域内每个路径函数少
写一个参数。

> ⚠ 命名坑：域文件顶部有 `from app.services.storage import _root`，把这个 helper 也叫
> `_root` 会遮掉模块名，`_root.path` 立刻 `AttributeError`。用 `_domain_root` 或 `_dir`。

### 3. `app/services/storage/tasks.py` — 把 step 目录当整体看的三件事（第 1 步）

3 个对外成员，每个都以身份键开头——**本包不提供任何脱离 `(task_id, step_id)` 的能力**
（这条正是 `lab_runtime_config.json` 出局的判据：它是配置，没有身份键）。

```python
def steps(task_id) -> List[int]
def ids(order: str = "id") -> List[int]          # "id" | "mtime"
def purge_step(task_id, step_id) -> bool
```

**本模块不出定位能力。** 中途曾有过一个 `tasks.path(task_id, step_id, domain, name,
create)`，在 `_root.path` 收成单入口后它只剩「join 一个 name + 一次 mkdir」——而按设计
它的**外部调用方是零**（每个外部调用方都走自己的域文件），内部调用方（域文件）在包内可以
直接用 `_root.path`。故删除。留下的三件事有共同特征：**跨所有域**，枚举的是目录本身、
删除的是整个 step。

**`ids(order=)` 合并了 `list_task_ids` / `list_task_ids_by_recency`**。两种顺序都是事实、
选错只影响清单顺序不会静默出错，故给默认值是安全的；非法 order 抛 `ValueError`——传错
说明调用方对返回顺序有预期，给它一个不符预期的顺序比报错更坏。

> `order="mtime"` 是**近似值，仅供挑深扫候选**，绝不对外当时间戳用。只 stat 目录、不进
> 目录读文件，成本 O(目录数)。无 step 子目录的 task 排序键取 0（排最后）但仍保留在结果里
> ——是否丢弃由调用方深扫时决定，不是本域的判断。

**`steps()` 不判断目录里有没有产物**：「两轨都没段的 step 算不算数」是 HLS 域知识。要过滤
的调用方自己用 `hls.segments()` 问；而 TTL 恰恰**不能**过滤——只有 `features.jsonl`、没有
HLS 段的 step 目录正是 #1 泄漏的那一类，它必须被看见。

**`purge_step()` 只执行不判断**：「重启 supersede」与「TTL 到期」是两个不同判断，分别留在
`persistence/manager` 与 `cleanup_worker`。它**删的是整个 step、三个域一起没**——docstring
与单测都把这条钉死，正是针对 #2 那个「删除者自述与行为不符」。刻意不提供 `purge_domain`：
两个真实场景都是整 step 粒度，单域删除目前零需求。父 task 目录若因此变空用 `rmdir` 一并
回收（对非空目录安全失败，无需先判空）。**不加锁**：并发保护是写侧机制，写者调它时仍在
自己的目录锁内。

**`ids(order="mtime")` 必须下钻到域子目录**——这是目录隔离带来的连带影响，不处理就会静默
退化：

> 产物落在 `{step}/{domain}/` 里，写一个段只更新 `hls/` 的 mtime，**step 目录本身纹丝不动**
> （它只在新建域目录那一刻变）。若排序仍只 stat step 目录，该值就退化成「该 step 首次落盘
> 的时刻」，一个刚写过新段的 task 会被排到后面，大屏历史的「最近」随之失准。故取「step 目录
> 及其各域子目录」mtime 的最大值。多一层仍是 O(目录数)：域数 ≤ 3，全程只 stat 目录、不进
> 目录读文件。`test_mtime_follows_domain_subdir_not_just_step_dir` 专盯这条。

三个候选成员被砍掉：

| 砍掉 | 理由 |
|------|------|
| `purge_empty_tasks()` | 并进 `purge_step`——删完 step 顺手检查父目录。比单独扫一遍更及时 |
| 临时件定位（曾拟名 `scratch_path()`；下文一律叫 **tmp**） | 能力太窄。调用方一行 `tasks.path(t, s, "hls", f".tmp_{token_hex(6)}.m3u8")` 就够，前导点约定写进 `path()` 的 docstring |
| `contained_path()`（path traversal 校验） | **不必要**。`/media/*` 的 filename 来自 HMAC 签名的 token、服务端签发时写入，路径校验是纵深防御不是唯一防线；且**正确的判据是校验名字不是校验路径**——合法 filename 只有 `{track}_segment_{ts}.mp4` / `{track}_init.mp4` 两种，那是 HLS 域知识，第 4 期用 `hls.parse_segment_name()` 判。执行形态见 §7.1 的 L2 |
| `lab.py` 的 `config_path()` | LS 运行时配置没有 `(task, step)` 身份键，不是产物（见 §1）。`lab.py` 缩到只管导出临时件 |
| `purge_domain()` | 单域删除零需求：「重启 supersede」与「TTL 到期」两个真实场景都是整 step 粒度 |

### 4. `tests/test_import_hygiene.py` — leaf 身份 + 导入预算（第 1 步）

新增两条：

```python
BUDGET["app.services.storage"] = (set(), 0.20)   # 对标 app.domain，stdlib-only 量级
LEAF_PACKAGES = ("app/services/storage",)        # AST 查：包内不得 import 其他 app.services.*
```

耗时上限卡 0.20s 是有意的：包名不自带约束，一旦有人往里塞 ffmpeg/cv2/批缓冲，这条会先红。

另两条门禁（`settings.storage_base_dir` 访问面收敛、`app/` 下除本包外不得出现文件名字面量）
**归第 4 期**——现在加会立刻红，因为调用点还没迁。

### 5. `app/services/__init__.py` — 顺带删掉零消费方的 re-export（计划外）

写门禁时发现 `import app.services.storage` 要 **280ms、拉起 494 个模块**，而包本身是
stdlib-only。定位到 `app/services/__init__.py` 的模块级：

```python
# 旧
try:
    from .client import client_manager
except ImportError:
    client_manager = None
```

**零消费方**（`grep` 全仓：`from app.services import client_manager` 与
`services.client_manager` 均无命中；`app/main.py:16` 那行 import 的是子模块，不走它），
却让**每一个** `app.services.*` 的导入都先付 282ms 与 455 个模块（client → numpy 链）的
过路费。这正是规范 §3 明令禁止的「中间态」——re-export 一堆便利符号却无活体，唯一效果是
把整棵子树的依赖变 eager。删除后改为标记型（纯 docstring）。

### 6. 保留项（刻意不改）

- **`app/services/traceback/segment_finder.py` 原样保留**，包括与 `_root.dir_name_to_int`
  重复的那份实现——本期是**复制不删除**，原处随第 4 期迁移一起删。这期间两份并存是自觉的
  临时状态，不是遗漏。
- **`app/settings.py` 不动**：`storage_base_dir` 的值与解析规则都留在它那里，本包只解析
  一次并缓存。
- **`hls_strategy` / `feature/store` / 各 router 全部不动**：本期零调用点改动。

### 7. 领域设计规范（定稿）：数据层的准入

> 约束本包所有域文件。第 1 期标「草稿」时的判据是「搬进来会不会让本包吃 L2 依赖或持有
> 状态」；第 2 期设计 `hls` 域时框架整个换代——storage 从「名字与定位」变成**数据层**，
> 据此重写。这不是补丁式回填，是判据换代，被推翻的条文列在 §7.0。第 3 期 `feature.py`
> 回填的路线 C 与 R6 一并并入。

**契约**：storage 是数据层——**内存数据模型与盘上数据之间的抽象**。调用方交出内存对象、
拿回内存对象或已定位的 `Path`；文件名、目录布局、文本格式、二进制布局、编解码，以及
保证这些不被并发写坏，全在层内。上层不碰其中任何一样。

#### 7.0 判据换代：进不进本层

| 问 | 归属 |
|------|------|
| 它是「内存模型 ↔ 盘上字节」的转换**本身**吗？ | **进层** |
| 它是关于这个转换的**策略**（要不要做、失败重试几次、产物留多久）？ | 不进 |
| 它是**编排**（谁调、何时调、排队、并行度）？ | 不进 |
| 它是**业务语义**（HTTP 状态码、告警阈值、多大算录制停顿）？ | 不进 |

一条容易滑过去的区分：**「失败了要不要重试」是策略，不进层；「这次失败是环境坏了还是
数据坏了」是层内事实，层必须能答**。旧 R3 的「不做业务判断」曾被延伸到失败分类上，
结果上层拿不到重试依据——每个数据层都在分这两档，数据库也是。

被本次换代推翻的条文：

| 原条文 | 处置 | 理由 |
|------|------|------|
| 「本包只管名字与定位，不管内容怎么读写」 | **推翻** | 编解码就是这一层的本职。留在层外，`duration_s`、tfdt 这类跨边界接线点就得靠人守，而它们写错**全是静默的** |
| 边界清单「ffmpeg 转码参数 / tfdt hex-patch / timescale pin / cv2 / eff_fps → hls_strategy 独家」 | **推翻**，移入本层 | 同上。`eff_fps` 同时决定媒体时长、EXTINF、tfdt 三个值，它在层外算，三个值就要分三条路跨边界 |
| 边界清单「目录锁 → persistence（并发机制不是格式）」 | **推翻**，移入本层 | 并发串行是数据层的不变式，不是调用方的义务；且锁的**正确粒度**取决于冲突域，而冲突域是格式知识（§7.4 C2） |
| D3 **不起子进程** | **推翻**，改 D3′ | 编解码可起外部工具，附三个条件 |
| W6「不保证跨调用原子，同一目标的并发由调用方序列化」 | **推翻** | 同「目录锁」条 |
| 「零状态」 | **部分推翻** | 锁表是状态。「不出句柄」仍然守（D4） |
| R3「不出领域异常」 | **保留**，补执行形态 | 多态失败用事实枚举 + NamedTuple 表达；失败分档见上 |
| 门禁映射（§7.7）里「②早于③」「②③同锁」两条 review 项 | **消失** | 三步塌进层内一次调用后，它们是实现细节，没有调用方能写错 |

判据换代**不放宽**边界：策略、编排、业务语义三类照旧不进，清单见包 docstring。
「包名叫 storage，听起来什么存储相关的事都能进」这条风险没有变小，只是分界线换了位置。

#### 7.1 读侧（R）

| 编号 | 规则 | 备注 |
|------|------|------|
| R1 | **出结构，不出字节、不出句柄** | 返回 dataclass / NamedTuple / ndarray / 基本类型；不出 `IO`、不出裸 `bytes` |
| R2 | **不出目录、不出存储根**；载荷字节的取用位置照出 `Path` | 出目录 = 把布局复制给调用方，且门禁抓不到（`dir / "x"` 是普通拼接） |
| R3 | **只解析本域格式，不做业务判断** | 阈值、该不该删、多大算录制停顿、映射成什么状态码，一律留调用方；不出领域异常。多态失败用**事实枚举 + NamedTuple**（`Vod(state, text, segments)`），不是 `Optional[str]` |
| R4 | **落盘状态是事实，可做过滤参数**；选错会静默出错的参数**必填、无默认** | `segments(..., playable_only=)` 必填（漏滤 → clip 静默截短，缺陷 #3）；`ids(order=)` 可给默认（选错只影响清单顺序） |
| R5 | **一次调用一次扫盘** | 按「一次调用 = 一件事」切函数；不照搬取值器，也不合成跨口径黑盒 |
| R6 | **环境坏了原样抛，内容坏了逐行隔离** | 打不开 / 写不进 / 建不了目录 → `OSError` 原样抛（「落盘失败要不要打断主链路」是调用方的判断）；单条记录解析不出 → 跳过并 warning，不让一行毁掉整个文件 |

**字节的转换归谁做**（R1/R2 的适用范围）——旧表的第三档随判据换代作废：

| 情形 | 归谁 |
|------|------|
| 只转发字节（`/media/*` 送 mp4 给浏览器） | 层出 `Path` |
| 有结构的编解码（`.idx` / playlist / `features.jsonl` / fMP4 fragment） | **层内转**，含为此起外部编码器（D3′） |
| **解码**（段 mp4 → 像素帧） | **层内转**（2026-09-11 拍板，见 §7.8） |

> 旧表第三档的判据是「单消费方或带策略参数 → 不进包」，它把编码方向也挡在了外面。
> 编码方向已随本次换代进层；解码方向是同一个对称问题，**已于 2026-09-11 一并拍板进层**
> —— 且落地后发现那一档假设的「抽帧率、内存上限由调用方定」并不成立：返回迭代器之后
> 内存上限自然归调用方，抽帧率则是伪需求（离线要的是墙钟区间，不是抽帧）。

#### 7.2 写侧（W）

三条路线。**调用方一律只交内存对象、只调一次函数**——路线是层内实现，不出现在签名上：

```text
路线 A  transaction（外部工具产出 / 有位置依赖）
    ① stage    锁外，层内起编码器在 {step}/{domain}/.stage_{产物键}/ 造产物
    ② adjust   锁内，读既有状态、做位置相关的最后修补（无位置依赖则 ①→③）
    ③ commit   锁内，原子 rename + 登记
  任一步异常 → 删 stage、不 rename、不登记，原异常上抛

路线 C  overwrite（层内纯序列化，整体替换）
    ① 编码     整批先编码完（失败时盘上一个字节没动）
    ② 写 tmp   与目标同目录的 .{name}.tmp
    ③ rename   os.replace 原子换名

路线 B  append（追加）
    收内存对象 → 序列化 → 一次 open(mode="a") + write
```

**A 与 C 的分界**：产物字节由**外部工具**造、或有位置依赖 → A；层内从内存对象**纯序列化**
→ C。旧版分界写的是「调用方造 → A / storage 造 → C」，编码进层后调用方不再造任何字节，
分界随之换成这条。

| 编号 | 规则 | 路线 |
|------|------|------|
| W1 | **stage / tmp 必须与目标同卷**（落在 `{step}/{domain}/` 之下），不得用 `tempfile.mkdtemp()` | A C |
| W2 | **目标文件名在 rename 前不得存在** —— 名字一出现就是合法产物 | A C |
| W3 | **位置相关的修补必须在 rename 之前** | A |
| W4 | **失败即整体作废**：删 stage/tmp、不 rename、不登记，原异常上抛 | A C |
| W5 | **一次调用写完调用方给的一批，层内不攒批** | B |
| W7 | **stage 目录名取「与产物同键」，不用随机 nonce** | A |
| W8 | **登记顺序：索引先于主产物可见，主产物先于清单条目** | A |

> W6（「不保证跨调用原子，同一目标的并发由调用方序列化」）已随判据换代删除，见 §7.4。
> 编号不复用——别的文档还在引 W1–W5。

**W7 的取舍**（`.stage_{track}_{ts_us}` 而不是 `.stage_{nonce}`）：

| 维度 | 随机 nonce | 与产物同键（采用） |
|------|------|------|
| 并发安全 | 恒成立 | 同键 = 同产物名，撞键在 `os.replace` 那步本就是既有 bug，不引入新的 |
| 崩溃残留 | 每次崩溃/重试留一个新目录，无人回收直到 TTL | 每段最多一份，**重试自然复用**（入口 `rmtree` 一次即幂等） |
| 可归因 | 目录名无信息 | 目录名即「哪一份产物没写完」 |
| 可测 | 要 mock `token_hex` 才能断言路径 | 直接断言 |

**W8 展开**（写错三条都不报错或错在下游）：索引类（sidecar）必须先于主产物可见——产物名
一出现就对读侧可见，反过来会留下「主产物可见但索引未就位」的窗口；主产物就位后才 append
清单条目——清单有行无文件 = 播放器直接报错；清单头（含 `EXT-X-MAP`）必须先于任何条目行。

**每类产物走哪条**：

| 产物 | 路线 | 理由 |
|------|------|------|
| `{track}_segment_*.mp4` / `{track}_init.mp4` | **A** | 外部编码器产出、字节量大；且有位置依赖（tfdt = 前面所有 EXTINF 之和，只有锁内才知道） |
| `{track}_playlist.m3u8` 的 `#EXTINF` 行 | **B** | 一行文本，可追加；作为 A 的 ③ 登记步执行 |
| `features.jsonl` | **B** | 追加型，没有"先造好一个完整产物"这回事 |
| `facts.jsonl` | **C** | 整批替换，内容由层内序列化 |
| `raw_segment_*.idx` | **B** | **具名例外**：可走 A，但它不匹配段正则、对读侧不可见、无中间态问题，走 A 只是多两步。必须写进域 docstring，否则后人当漏改 |

#### 7.3 定位（L）

```text
查询 / 外部输入 ──▶ 结构 ──▶ 定位 ──▶ Path ──▶ 外部工具读写
                    ↑ 校验在这一步
```

| 编号 | 规则 | 备注 |
|------|------|------|
| L1 | **定位函数收结构，不收散标量** | `segment_path(t, s, ref)`；写侧自己构造 `SegmentRef` 即可，读写两侧共用一个函数。顺带消掉现存分叉：写侧从 ts 拼 `.idx` 名、读侧 `with_suffix(".idx")` |
| L2 | **外部输入先经 `parse_*` 转结构，路径由结构重建** | 外部字符串从不进入路径 → path traversal 结构上不可能（§3 砍 `contained_path()` 的执行形态） |
| L3 | `SegmentRef` **带 `track`、不带 `task_id`/`step_id`** | track 在文件名里、`parse_segment_name` 必须解出；两个 id 是路由键，调用方手里有 |
| L4 | **域根一律经 `_root.path()`**，域名在一个域文件里只出现一次 | 顶部 `_DOMAIN = "hls"` + `_domain_root()`，样板见 §2；helper 不能叫 `_root`（会遮模块名） |
| L5 | **域文件不得自己读 settings、不得自己拼根** | 根解析只有 `_root._storage_root()` 一处 |

#### 7.4 ~~并发（C）~~ —— **整节已作废（2026-09-11）**

> **本节的全部结论被推翻，C1–C7 一条都不再有效。** 替代方案见
> [20260911_STEP_INIT_SUPERSEDE](20260911_STEP_INIT_SUPERSEDE.md)，执行形态见
> [20260911_STORAGE_HLS_DOMAIN](20260911_STORAGE_HLS_DOMAIN.md) §4。
>
> **推翻的理由不是"锁放错了层"，是"这里根本不该用锁"**：锁保证的是「不重叠」，而 supersede
> 需要的是「旧 run 已终止」——一个还在写的 run，锁只能让 `rmtree` 等一下，等完了它继续写，
> 僵尸 step 照样出现。**互斥挡不住一个没停的写者。**
>
> 新的分工是两句话：**同代次内的顺序由 `SerialTaskQueue` 的提交序保证，跨代次的隔离由
> owner 判等（乐观锁，失败即丢弃）保证。** 本层一把锁都不持，也不再宣称任何并发保证——
> C1 的「写入串行由层内保证」随之改为「**串行由调用方的单写者队列保证，层只做转换**」。
>
> 连带作废：§7.0 推翻表里「目录锁移入本层」那一行（移入的判断本身是错的）、D5 上方 D4 里
> 「可持有并发协调设施（§7.4 的锁表）」的括号内容、以及 §8 整节（`_locks.py` 已删）。
>
> 以下原文保留作推翻记录，**不要照它实现**。

判据换代新增的一节。**并发串行是数据层的不变式**，和数据库一样：调用方不持锁、不传锁、
不知道有几把锁。

| 编号 | 规则 | 备注 |
|------|------|------|
| C1 | **写入的串行由层内保证**，锁不出现在签名上 | 锁作参数传进来是把互斥的实现细节漏给调用方；锁留在调用方则「为什么要锁」的理由在层内、执行在层外，正是本次要消灭的那类分裂不变式 |
| C2 | **默认一把 per-`(task, step)` 的锁，所有域与 `purge_step` 共用**；更细的粒度要有实测支撑才拆 | 冲突域是格式知识、只有层内知道，所以**粒度归层**——但粒度的默认值该由实测定。当前并发面：HLS worker 2 个（可配 1–4），竞争只发生在同 step 的 raw / processed 两轨之间，transcode ~260 ms、段间隔 ~10 s，占空比约 3%。拆到 per-track 只省这 3% 的一半，不值一套自定义读写锁 |
| C3 | **保证的作用域必须写进域 docstring** | `threading.Lock` 只在**进程内**有效。多进程（`uvicorn --workers>1`）下不报错、不告警，只是静默写坏。要换文件锁（`fcntl` / `msvcrt.locking`）时，升级路径也归本层 |
| C4 | **锁表按 task 可回收** | 不回收则随 `(task_id, step_id)` 单调增长，长跑慢泄漏。回收后在途者若再取锁会按需重建同一把，不影响串行正确性（该论证已在 `hls_strategy.release_dir_locks` 验证过） |
| C5 | **锁内只做必须在锁内做的事** | 编码（外部工具、几百 ms）在锁外的 stage 步；锁内只有「读既有状态 → 位置相关修补 → rename + 登记」。把编码圈进锁 = 同一 step 的多轨写入串行 |
| C6 | **不加已被锁序列化的守卫** | 被锁覆盖的路径上再放 `_inflight` 之类的状态，是一层永不触发、白付的复杂度 |
| C7 | **`purge_step` 与 step 初始化取的是同一把 step 锁**，因此天然排掉该 step 下所有域的在途写 | 单域锁挡不住跨域删除。现状是两把互不相关的锁：`purge_step_dir` 持 hls 的目录锁，`FeatureStore` 的 append 持 `store.py` 的 `_lock`——`rmtree` 与 append 之间零互斥，表现是某域目录删到一半，或写侧 `create=True` 在 rmtree 后重建目录、留一个已被记账删除的僵尸 step，**都不报错**。C2 合并成一把锁后，这个洞不用单独一条机制就没了 |

**执行形态：一把普通 `threading.Lock`，`storage/_locks.py` 全层共用。**

```text
域内写         with _locks.step(task_id, step_id):  ...
purge / 初始化  同上，同一把锁
```

曾考虑过 per-`(task, step)` 读写锁（域内写取共享、purge 取独占），以保住 C2 的 per-track
粒度。**否决**：stdlib 没有 RWLock，要自己写（计数 + `Condition`，约 30 行）加全部调用点，
而它买到的是那 3% 占空比里的一半，外加一个触发条件极窄的竞争（见下）。合并成一把锁之后
实现只剩表 + guard，比**现状**还简单——现状是两把互不相关的锁。

代价是 raw / processed 串行，而它们**今天本来就串行**（现状 `_get_dir_lock` 的 key 就是
step 目录）。真到吞吐吃紧那天再拆，那时会有实测数字支撑粒度选择。

> C1 的边界：层保证的是**同一个 step 内的写入互斥**，不是分布式事务，也不跨 `(task, step)`。
> 跨 step 的顺序、什么时候写、写不写，仍是编排方的事（§7.0）。
>
> **owner fence 不在此列**：`features.jsonl` 的 `open_fresh` 要做「run 身份校验 → unlink」
> 且两步之间不能被插入，那把锁罩的是 run 身份（层外语义），归 `store.py`。它与层内的域锁
> 保护的是两件不同的事——层锁挡 purge 与并发写，store 锁挡跨 run 串台——不是两把锁护同
> 一个不变式。

#### 7.5 依赖与状态（D）

| 编号 | 规则 |
|------|------|
| D1 | **模块级默认 stdlib-only**，重依赖走 D2。**唯一例外是「域货币」**：该域每个签名都在收发的类型（`feature` 的 `FrameFeature`，随之带进 numpy），它没法推迟到函数体内。例外必须在 `BUDGET` 里单独登记上限。收紧自初版「域文件可吃 L1 numpy」——`hls` 域 14 个成员里只有 1 个出 `ndarray`，那不是货币、走 D2，别让另外 13 个的调用方陪付 116 ms。子包 facade 的 re-export 会连带加载实现模块，这条因此也是子包形态能成立的前提 |
| D2 | 单个重函数要的依赖走**函数体内 import**，别让整个域为它变重 |
| D3′ | **编解码可起外部工具子进程**（推翻初版 D3），附三个条件：① 必须有超时；② 失败即整体作废（W4），不留半成品；③ 只用于编解码——调度、编排、并行度一律不进层 |
| D4 | **可持有**解析缓存、并发协调设施（§7.4 的锁表）；**不可持有**批缓冲、待写内容、owner fence、连接、任何要 `start()/stop()` 的东西。**不出句柄**这条不变（设计约束 4） |
| D5 | **外部工具的可用性是运行时依赖，不是 import 期依赖**：`import app.storage.hls` 不得要求机器上有 ffmpeg；缺二进制在调用 `write_segment` 时才炸，读侧不受影响 |

#### 7.6 测试（T）

| 编号 | 规则 |
|------|------|
| T1 | **每个 codec 必须有往返测试** —— 这是转换契约唯一能被验证的地方，也是「正反运算同居」的兑现。层的契约是「内存模型 ↔ 盘上数据」，往返链条随之变长：写进去的内存对象读回来要相等 |
| T2 | `ts_to_us` 的往返断言在 **us 域**闭合：`parse(name(ts)) == int(ts * 1e6)`，**不是** `== ts`。截断有损，读侧段级定位的 `bisect_right - 1` 正建立在它上面 |
| T3 | **事务不变式必须能脱离外部工具测**：保留私有的 commit 入口，喂手工造的最小合法产物，断言 W2 / W4 / W8。否则最该测的断言躲在需要 ffmpeg 的函数背后，CI 缺二进制时静默失效 |
| T4 | **需要外部二进制的测试单独分档**（marker），与层内纯 tmpdir 测试分开跑。层的主体测试必须毫秒级、无外部依赖——它是所有人的下游，跑得慢就没人跑 |
| T5 | **并发有一条真测试**：多线程同时写同一冲突键，断言产物齐备、清单行数正确、位置相关字段（如 tfdt）不碰撞。这是 C1 唯一能被验证的地方；测试碰私有面在 T3/T5 是正当的——被测的是内部不变式，不是公开契约 |

#### 7.7 门禁映射

| 规则 | 可执行性 |
|------|---------|
| L4 域名白名单、`domain` 必填 | ✅ 已有 |
| 依赖白名单（只许 `app.storage` / `app.domain` / `app.settings`） | ✅ 已有（§9 把黑名单换成白名单，反向探针验过） |
| D1 模块级 stdlib-only | ✅ 逐模块 `BUDGET` + 覆盖检查（§9 已落地）。域的重依赖集合登记为空，塞 cv2 到模块级当场红 |
| T1 / T2 往返测试 | ✅ 每个 codec 一条，写不出来说明正反运算没同居 |
| T3 事务不变式 / T5 并发 | ✅ 新增。T5 是概率性的，但没有锁时必红 |
| L2 外部输入经 `parse_*` | ✅ `parse_segment_name` 对 `../`、绝对路径、非法 track、非数字 ts 返回 `None`；`segment_path` 入参类型也不收裸 `str` |
| R4 正确性参数必填 | ✅ 漏传 `TypeError` |
| C1 锁不出现在签名上 | ✅ 写侧公开成员的签名里不得有锁类参数，AST 可扫；配合 T5 |
| D3′ 子进程必须带超时 | ✅ AST 扫 `subprocess.*` 调用有无 `timeout=` |
| L5 `settings.storage_base_dir` 访问面收敛 | ⏳ 第 4 期（现在加会立刻红） |
| 文件名字面量不出层 | ⏳ 第 4 期。**AST 扫 `ast.Constant` 并跳过 docstring**，不能 grep（`_segment_` 会命中 30 多处 `ca_segment_len`） |
| R1 / R3 | ⚠️ 靠返回类型标注 + review |
| C3 作用域声明写没写 | ⚠️ review（docstring 里有没有那句「本层只保证进程内」） |
| D3′ 的另两个条件（失败作废、只用于编解码） | ⚠️ review |
| **W 的路线选择（A / B / C）** | ❌ **不可测，只能 review**。这是本规范最需要人看的一条：走错不会红，表现是「中间态被读到」，而那正是静默失败 |

> 换代前「②必须早于③」「②③必须同锁」两条曾挂在这张表的 ❌ 档，要求写侧 PR 在描述里
> 写明。三步塌进层内之后它们**不再是调用方能写错的东西**，已从表中移除——这是判据换代
> 在门禁上的直接兑现。

#### 7.8 ~~未决~~：读侧对称（解码方向）—— **已拍板：进层**（2026-09-11）

> **结论**：选「现在定形态」那一列。`hls.read_segment` / `hls.iter_frames` 已落地，
> 与 `insert_segment` 互为逆运算，**T1 的往返闭合到段这一档**（ts 位级相等 + 像素逐帧
> 匹配，真 ffmpeg 验过）。落地记录见
> [20260911_STORAGE_HLS_DOMAIN.md](20260911_STORAGE_HLS_DOMAIN.md) 的「读向对称」追加节。
>
> 三点与下表预估不同的实际结果：
>
> - **没有"抽帧率 / 内存上限"这类策略参数**。返回 `Iterator[Frame]`，内存上限自然归调用方
>   （要整段进内存自己 `list()`）；抽帧率是伪需求——离线要的是"这个墙钟区间的帧"，
>   给的是 `start_ts` / `end_ts`。唯一较真的是 `width` / `height` **不给默认值**（约束 5）。
> - **`Timeline` / `FrameTracker` 本期零改动**。解码是复制进域、不是移动，两份并存到写侧
>   切到 `{step}/hls/` 那一刻；改造面因此没有兑现。
> - **解码只服务 raw 轨**。processed 不落 sidecar 是既有的有意不对称，故 `iter_frames`
>   连 `track` 参数都不设，`read_segment` 收非 raw 的 ref 直接 `ValueError`。
>
> 留在层外的是 `FrameTracker.find` 的**位级 ts 匹配**：「ts 是帧的身份，配错帧比报错更坏」
> 是业务语义，按 §7.0 四问的第四条不进层。

「数据层」是双向的：写进去 `Sequence[Frame]`，读出来该是 `Frame`。编码方向随本次换代
进层，解码方向当时仍判给 `inference/offline`，层只出 `segment_path` 与帧 ts 数组。
以下是拍板前的原文，保留以记录当时的两个选项：

| 选项 | 代价 | 影响 |
|------|------|------|
| ✅ **现在定形态、第 6 期迁** | 解码带策略参数（抽帧率、内存上限），签名要设计；`Timeline` / `FrameTracker` 改造面比写侧大 | 层对称，`read_frames` 与 `write_segment` 互为逆运算，T1 往返可闭合到内存对象 |
| 显式承认只做写不做读 | 层只会序列化不会反序列化，`__init__` 边界清单要写明这是**刻意的不对称**而非遗漏 | 第 6 期不用动 offline；但 T1 的往返在段这一档永远闭合不了 |

**在拍板之前，不要按旧 §7.1 第三档默认它留在层外**——那条判据（「单消费方或带策略参数
→ 不进包」）已随本次换代作废，它当初把编码方向也一起挡在了外面。

### 8. ~~`app/storage/_locks.py` —— step 级写入锁~~（2026-09-09 追加，**2026-09-11 已删除**）

> **本节已作废**：`_locks.py` 与 `tests/test_storage_locks.py` 随 `SerialTaskQueue`
> （[`app/utils/task_queue.py`](../../app/utils/task_queue.py)）的落地而删除——串行由**提交序
> 构造**比抢锁更强，且顺带覆盖「旧残段先落盘、再整个删掉」。本层此后一把锁都不持，写侧
> 的并发前提落在各域写入口的 docstring 里。原文保留只为记录当时的方案。

包内私有，stdlib only，3 个成员：`_get` / `step`（上下文管理器）/ `release_task`。
一把 per-`(task_id, step_id)` 的 `threading.Lock`，键经 `int()` 归一（路由与 DB 两侧的
id 类型历史上不完全一致，不归一就会出现「两个写者各持一把锁写同一个目录」，静默失效）。

**`step()` 只交出上下文、不交出 `Lock` 对象**——拿到锁对象就能存起来、传出去，那正是
设计约束 4「不出句柄」要挡的。

**用 `Lock` 不用 `RLock`**：持锁期间再调一个同样要取锁的层内函数会死锁。这是有意的——
要防的是「域内写到一半顺手调 `purge_step`」，`RLock` 会让它默默通过、把自己正在写的目录
删掉；死锁至少停在那里、栈上看得见。

**本期只落模块，不接调用点**：各域的写入口与 `purge_step` 改为经它，随第 4 期各域迁移
一并做。`tests/test_storage_locks.py` 13 条覆盖身份归一、互斥、异键不阻塞、异常路径放锁、
在途回收——端到端的 T5（多线程写同一 step、断言产物与位置相关字段）留给域的写侧测试，
那里才有产物可断言。

### 9. 迁址 `app/services/storage/` → `app/storage/`（2026-09-09 追加）

**它不是一个服务，是数据层。** 放在 `app/services/` 里，「它不许依赖任何服务」是一条需要
写 8 行 docstring 解释的例外；放在 services 平级、下面一层，同一条规则不需要解释。
`app/domain/` 已经是这个位置的现成先例。

**现在做几乎免费**：迁址时 storage 零生产调用点——引用它的只有包内 3 个文件、2 个测试、
`BUDGET` 4 行、`LEAF_PACKAGES` 1 行、文档若干。第 4 期接上 10 个调用点之后再搬，代价贵一
个量级。故当作第 1 期的收尾单独提交，diff 全是路径字符串。

```text
app/
  domain/     内存数据契约（Frame / FrameFeature）
  storage/    ← 数据层：内存模型 ↔ 盘上字节
  services/   业务服务，单向依赖 storage
  routers/
```

**薄域用单文件、重域用子包，对外看不出区别。** `from app.storage import hls, feature` 拿到
的都是一个域的公开面，调用方分不出 `hls` 是包还是模块——所以「薄域将来变重」是**非破坏性
升级**。第 2 期的 `hls` 因此定为子包（`hls/__init__.py` facade + `_m3u8` / `_fmp4` /
`_encode`），`feature.py` / `lab.py` / `tasks.py` 维持单文件。

代价一条：子包的 `__init__` 是 facade 不是标记型，re-export 会连带加载实现模块，故那些模块
的模块级必须保持 stdlib-only（cv2 走函数体内 import），`app.storage.hls` 的预算才能维持
「重依赖集合为空」。已写进 `BUDGET` 的注释与包 docstring。

**门禁三处改动**：

| 项 | 改动 | 理由 |
| --- | --- | --- |
| `LEAF_PACKAGES` → `LAYER_PACKAGES` | 从「路径元组 + 黑名单 `app.services.*`」改成「路径 → 允许的 `app.*` 前缀」字典，storage 的白名单是 `app.storage` / `app.domain` / `app.settings` | 黑名单挡不住 `app.database` / `app.models`。它们进来不造环、不报错，只会把数据层绑死在 ORM 上，等到想换存储那天才发现 |
| `test_storage_modules_are_all_budgeted` 的模块名推导 | `as_posix()` 直接拼 → 按 `rel.parts` 拼、各级 `__init__` 折叠 | 原实现遇到子包会生成 `app.storage.hls/__init__` 这种带斜杠的非法模块名。测试会红，但会诱导人把带斜杠的 key 加进 `BUDGET`，然后 `import_module` 那步才炸。子包形态落地前必须修 |
| 相对 import 放行 | 白名单检查跳过 `node.level > 0` | 包内寻址天然合规，子包里用相对 import 是常态 |

两道门禁都做过反向验证：往 `app/storage/` 塞一个 `import app.database` +
`from app.services.persistence import instance` 的探针文件，白名单门禁与预算覆盖门禁**同时
变红**并点名到行。全量 `pytest tests/` 516 passed。

**包 docstring 同步更新**：头部的「零跨服务依赖的 leaf」换成位置声明 + 依赖白名单；
「对外契约」补薄/重两种形态；设计约束 1 从「一域一模块」改为「一域一个 import 名」。
边界清单当时留了个尾巴——**已在本次规范定稿时补齐**：判据从「会不会吃 L2 依赖或持有
状态」换成「是不是内存模型 ↔ 盘上字节的转换」，清单里 ffmpeg / cv2 / tfdt / 目录锁四行
随之移入本层，见 §7.0 的推翻表。

## 变更效果

| 维度 | 变更前 | 变更后（设计；实际切换在第 4 期） |
|------|--------|--------|
| 落盘布局的公共下游 | 无（写侧不敢依赖读侧的 `traceback`） | `app/services/storage`，leaf 门禁锁死 |
| step 目录内产物 | 全部平铺，谁的文件谁的说不清 | `hls/` `features/` `lab/` 三域隔离，**step 根下无文件** |
| 存储根下的住户 | 数字 task 目录 + `.lab_exports/` + `lab_runtime_config.json` | **只有数字 task 目录** |
| 域隔离的执行力 | —— | 白名单（打错 `ValueError`）+ 各域文件绑死自己的域名，结构性保证而非约定 |
| `_root` 的定位面 | —— | 一个 `path(task_id, step_id, domain, *, create)` 逐级下钻，替掉 `root`/`task_dir`/`step_dir`/`domain_dir` 四个名字；`tasks` 侧不再有第二个定位入口 |
| LS 运行时配置 | 寄居存储根（`{root}/lab_runtime_config.json`） | 降级为 `settings` 路径字段，storage 包不认识它 |
| lab 导出临时件 | `{root}/.lab_exports/` + 自写 30 分钟孤儿扫描 | `{task}/{step}/lab/`，随 step TTL 回收 |
| `import app.services.storage` | —（不存在） | **2 ms，41 个模块，重依赖集合为空** |
| `import app.services.client` | 282 ms / 494 模块 | 不变（它本就要 numpy）；但**不再是所有 `app.services.*` 的过路费** |
| `app.services` 包 `__init__` 副作用 | 模块级 re-export 活体单例 | 零副作用（标记型） |
| task/step 目录能力的调用形态 | `SegmentFinder(get_default_base_dir())` ×10 处构造 | `tasks.path/steps/ids/purge_step` 模块函数（**第 4 期才接上**） |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_storage_tasks.py`（新增） | 44 passed —— `_root.path` 逐级下钻/跳级报错/白名单三档（合法域、笔误域、路径逃逸）、根解析记忆化失效、`tasks.path` 的 domain 必填/域不碰撞/校验早于 mkdir/`create` 只建自己那个域、`steps`/`ids` 三种顺序与跳过非 id 目录、**`ids(order="mtime")` 跟随域子目录**、`purge_step` 六种情形（含"三个域一起删"与父目录回收/保留） |
| `tests/test_import_hygiene.py` | 9 passed（原 7 + 导入预算新增 1 项 + leaf 门禁 1 条） |
| 全量 `pytest tests/` | **485 passed**，零 failed。改动纯增量：既有测试文件只碰了 `test_import_hygiene.py` 且 diff 为 `+41 −0`，无任何既有用例被修改或删除 |
| 运行时行为 | 未验证也无需验证——本期不改任何生产调用路径 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **落盘结构是 breaking 变更，无迁移路径** | 第 4 期切换的那一刻，现有 `database/` 下所有数据全部读不到——回放、送标、离线反查同时失效。dev 与已部署环境需要**手动清空 `database/`** | 符合本仓既有立场（[20260902_LEGACY_LAYOUT_CLEANUP](20260902_LEGACY_LAYOUT_CLEANUP.md) 已确立「旧落盘结构一律不做兼容、无迁移路径」，且 step 目录本就随 TTL 回收、每 run 重生）。但**清空时机需要人拍板**，第 4 期落地前必须先确认 |
| **`settings` 的 LS 配置路径字段尚未新增** | `lab/config.py:37-41` 仍在 `from ...segment_finder import get_default_base_dir` 拼 `{root}/lab_runtime_config.json`——即 LS 配置目前仍寄居存储根 | 第 4 期随调用点迁移一并做：加 `settings.lab_config_path`（str 字段 + env 覆盖），`lab/config.py` 改读它。默认值待定，我倾向 `./config/lab_runtime.json` + `.gitignore` + DEPLOYMENT 注明「运行时生成，勿覆盖」；若嫌部署覆盖风险，改独立 `runtime/` 目录 |
| **已落盘的 `lab_runtime_config.json` 会成孤儿** | 换路径后旧文件读不到，用户在页面上设的 LS URL / project_id / task_source 回退到 env 默认值 | 影响面小（三个字段，页面上重设一次即可），但**升级说明里要写一句**，否则表现为"升级后送标配置莫名其妙被重置" |
| ~~**§4 的导入预算门禁盖不住域文件**~~（已消解） | `BUDGET` 只登记包名 `app.services.storage`，而本包是标记型 `__init__`、零 re-export——`import app.services.storage` 不加载 `hls.py`/`tasks.py`（实测建了 `tasks.py` 后仍是 1 ms / 41 模块）。故「往里塞 ffmpeg/cv2 这条会先红」**不成立** | **第 3 期已补**：BUDGET 逐模块登记（`_root` / `tasks` stdlib-only 0.20 s、`feature` 允许 numpy 0.40 s），外加 `test_storage_modules_are_all_budgeted` 覆盖检查——新域文件漏登记就红。详见 [FEATURE_DOMAIN §6](20260909_STORAGE_FEATURE_DOMAIN.md) |
| ~~**§7 规范是草稿**~~（已定稿，但代价是一次判据换代） | 初版条文从「storage = 名字与定位」反推；第 2 期设计 `hls` 域时框架整个换代成**数据层**，编解码 / 外部工具子进程 / 目录锁三类从「不进包」变成「本层本职」 | **已重写为 §7 定稿**（含第 3 期 `feature.py` 回填的路线 C 与 R6），推翻记录在 §7.0。§7.4 并发节写完当场被 `feature` 域校正出 C1′ 例外条款。仍未被任何域文件验证过的是 R4 / T2 / L1-L3 与 C2–C6——它们全部押在 `hls` 域上，第 2 期落地时若再有回填，按同样方式改 §7 正文而不是打补丁 |
| **跨域删除与在途写之间零互斥**（窄，非 P1——初判有误已订正） | `purge_step` 的「不加锁」前提只对 supersede 成立。但触发面比初判窄得多：**TTL 路径撞不上**（删的是 `updated_at` 超过 `cleanup_days` 天的 step，活跃写入的 step `updated_at` 是现在，时间轴上差几天）；**supersede 路径已被现有机制覆盖**（features 侧有 owner fence 的「unlink 前入文件随后被删 / unlink 后被 owner 拒」二择一论证，hls 侧 `purge_step_dir` 自身就在目录锁内）。剩下的是理论窗口，不是在跑的 bug | **已换代解决，不再用锁**（2026-09-11）：`_locks.py` 从未接线即被删除。supersede 改为域内懒惰清理（各域写者首写自清），跨代次隔离靠 owner 判等，跨域删除只剩 TTL 一处且无并发写者——方案见 [20260911_STEP_INIT_SUPERSEDE](20260911_STEP_INIT_SUPERSEDE.md)。本行左栏「触发面很窄、不是在跑的 bug」的判断仍然成立，只是处置换了 |
| **§7.2 的路线选择（A/B/C）不可门禁化** | 走错路线不会红：该走 transaction 的走了 append，表现是"中间态被读到"，而那正是静默失败 | 只能靠 review。已在 §7.7 显式标注为「本规范最需要人看的一条」，新增域文件的 PR 必须在描述里写明每类产物选了哪条路线及理由 |
| **本期代码无生产消费方** | 新包是死代码，只有单测在跑；若第 2-4 期停摆，它会成为无人维护的孤儿 | 分期的自觉代价。第 4 期迁移是本次改造的价值兑现点，不宜长期搁置 |
| **`dir_name_to_int` 两份并存**（`_root` 与 `segment_finder`） | 期间若有人改其中一份，两份行为分叉 | 第 4 期删 `segment_finder.py` 时消解。两份都是「非数字返回 None」的三行实现，分叉风险低 |
| **`purge_step` 合并空目录回收后有一处行为差**（第 4 期迁移时才显现） | 现在 `cleanup_worker:99-113` 兜底回收**任何**空 task 目录（含因手工删除、重启 supersede 变空的）；合并后只回收"本次 purge 导致变空"的，残留空目录不再有清理者 | 接受：零字节，且 `ids()` 会列出它但 `steps()` 返回 `[]`，调用方本就会丢弃。第 4 期在迁移记录里再点一次 |
| **`ids(order="mtime")` 的近似性因分层加深而变松** | 它现在取「step 目录 + 各域子目录」mtime 的最大值。lab 导出临时件写进 `{step}/lab/` 也会刷新该值，于是"看了一眼回放"可能把一个老 task 顶到清单前面 | 接受：本就是**仅供挑深扫候选**的粗排，docstring 与用例都写死了"绝不对外当时间戳用"。真要精确，第 6 期的活动标记（`.activity` 或等价物）才是正解 |
| **`app/services/__init__.py` 的 re-export 删除未经端到端验证** | 理论上若有动态取属性（`getattr(app.services, "client_manager")`）会 `AttributeError` | `grep` 已确认零静态引用，全量 473 用例通过（含 `app.main` 导入预算用例）。真正的兜底是第 4 期后那轮 dev 启停 |
| **三条已知缺陷（#1/#2/#3）本期一条未修** | `features.jsonl` 仍在泄漏；clip 仍可能静默截短 | #3 → 第 5 期，#1 → 第 6 期，#2 随第 4 期 `purge_step` 归位一并消解 |
| **dev 端到端启停未做** | 本期不改运行时路径，理论上无影响 | 留到第 4 期迁移完成后一次性做。**会连真实 DB，跑前先与人确认**（见 [CLAUDE.md](../../CLAUDE.md)） |
