# 抽 `app/services/storage/` leaf 包：基础能力 + 产物按域隔离目录，零调用点改动

> **变更状态**：生效中（2026-09-09）　<!-- 新包已落地并有单测/门禁覆盖；本期刻意不接调用点，包内代码暂无生产消费方。落盘目录结构的实际变更发生在第 4 期迁移那一刻 -->
> **知识库**：待沉淀
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
2        建 hls.py + _m3u8.py + 单测                             零调用点改动
3        建 feature.py + lab.py + 单测                           零调用点改动
4        迁移调用点 + 删 segment_finder.py
         + traceback 改名 media_access + 门禁②③                 **落盘结构在此切换**
5        修 #3 clip_builder 在途段                               行为变更，独立评审
6        修 #1 TTL 判据换代 / metadata.json 存废                  格式变更，独立立项
```

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
| `scratch_path()` | 能力太窄。调用方一行 `tasks.path(t, s, "hls", f".clip_{token_hex(6)}.m3u8")` 就够，前导点约定写进 `path()` 的 docstring |
| `contained_path()`（path traversal 校验） | **不必要**。`/media/*` 的 filename 来自 HMAC 签名的 token、服务端签发时写入，路径校验是纵深防御不是唯一防线；且**正确的判据是校验名字不是校验路径**——合法 filename 只有 `{track}_segment_{ts}.mp4` / `{track}_init.mp4` 两种，那是 HLS 域知识，第 4 期用 `hls.parse_segment_name()` 判 |
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
| **本期代码无生产消费方** | 新包是死代码，只有单测在跑；若第 2-4 期停摆，它会成为无人维护的孤儿 | 分期的自觉代价。第 4 期迁移是本次改造的价值兑现点，不宜长期搁置 |
| **`dir_name_to_int` 两份并存**（`_root` 与 `segment_finder`） | 期间若有人改其中一份，两份行为分叉 | 第 4 期删 `segment_finder.py` 时消解。两份都是「非数字返回 None」的三行实现，分叉风险低 |
| **`purge_step` 合并空目录回收后有一处行为差**（第 4 期迁移时才显现） | 现在 `cleanup_worker:99-113` 兜底回收**任何**空 task 目录（含因手工删除、重启 supersede 变空的）；合并后只回收"本次 purge 导致变空"的，残留空目录不再有清理者 | 接受：零字节，且 `ids()` 会列出它但 `steps()` 返回 `[]`，调用方本就会丢弃。第 4 期在迁移记录里再点一次 |
| **`ids(order="mtime")` 的近似性因分层加深而变松** | 它现在取「step 目录 + 各域子目录」mtime 的最大值。lab 导出临时件写进 `{step}/lab/` 也会刷新该值，于是"看了一眼回放"可能把一个老 task 顶到清单前面 | 接受：本就是**仅供挑深扫候选**的粗排，docstring 与用例都写死了"绝不对外当时间戳用"。真要精确，第 6 期的活动标记（`.activity` 或等价物）才是正解 |
| **`app/services/__init__.py` 的 re-export 删除未经端到端验证** | 理论上若有动态取属性（`getattr(app.services, "client_manager")`）会 `AttributeError` | `grep` 已确认零静态引用，全量 473 用例通过（含 `app.main` 导入预算用例）。真正的兜底是第 4 期后那轮 dev 启停 |
| **三条已知缺陷（#1/#2/#3）本期一条未修** | `features.jsonl` 仍在泄漏；clip 仍可能静默截短 | #3 → 第 5 期，#1 → 第 6 期，#2 随第 4 期 `purge_step` 归位一并消解 |
| **dev 端到端启停未做** | 本期不改运行时路径，理论上无影响 | 留到第 4 期迁移完成后一次性做。**会连真实 DB，跑前先与人确认**（见 [CLAUDE.md](../../CLAUDE.md)） |
