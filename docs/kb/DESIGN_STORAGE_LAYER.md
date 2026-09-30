> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据层 `app/storage/` 设计规范

**本文是准入判据，不是现状描述。** 新增域文件、新增成员、review 存储层 PR 时按它判。

> **适用范围**：本文给的是全层通用的判据，**不逐域记录哪个域已有生产调用点**——那是过程
> 状态，盘上此刻长什么样见
> [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。
>
> **条文编号是代码引用的一部分**：R/W/L/D/T、路线 A/B/C、设计约束 1–8 被多处 docstring 与
> 测试直接引用，**改条文可以，改编号要先搜引用**。代码里残留的 `§7.x` 写法见 §10 对照。

---

## 0. 一张图：层在哪、经手什么

```text
调用方（services / routers）
   │  交内存对象 + RunIdentity                      拿内存对象 / 已定位的 Path
   ▼                                                        ▲
┌────────────────────────────────── app/storage/ ──────────────────────────────────┐
│  hls/            inference/                 lab(未建)   tasks.py     runs.py      │  §1 分域
│   types.py        (FrameDetection/LabelProbs)            (跨域枚举)   (分配/查询)  │  §2 域容器
│   _write / _read / _encode / _decode / _m3u8 / _fmp4 / _idx / _timeline          │  §4 §5 转换
│   hls._layout.domain_dir ───────┐                                                │
│   inference._layout.domain_dir ─┴─▶ utils.root.domain_dir(run, domain, create)   │  §3 定位
│   utils.fs   整体替换 / 原子删除 / 建一级目录（全层唯一一份盘上原语）           │
└──────────────────────────────────────────────────────────────────────────────────┘
   ▼
{root}/{task}/{step}/{run_id}/{hls|inference}/   域名过白名单；step 下只有 run 目录，run 下只有域目录
```

**契约一句话**：storage 是**内存数据模型与盘上数据之间的抽象**。调用方交出内存对象、拿回
内存对象或已定位的 `Path`；文件名、目录布局、文本格式、二进制布局、编解码全在层内，上层
不碰其中任何一样。层是**双向**的——写进去 `Sequence[Frame]`，读出来是 `Frame`。

**位置**：`app/storage/`，与 `app/services/` 平级、在它下面一层。它不是服务，是数据层；
放进 `services/` 里，「它不许依赖任何服务」就成了需要写 8 行解释的例外。`app/types/`、
`app/db/` 是同一层级的先例。

**依赖白名单**：stdlib、三方，以及 `app.types`（内存契约）与 `app.settings`（落盘根的唯一
来源，只在函数体内 import）。别的 `app.*` 一律不行——包括 `app.db`：它进来不造环、不报错，
只会把数据层绑死在 ORM 上。门禁
`tests/test_import_hygiene.py::test_layer_package_imports_only_whitelisted_app_modules`。
这条是写侧 recording / run_control / inference.offline 与读侧 lab / routers、以及 cleanup
daemon 能**同时向下依赖它**的前提——一旦它向上或向旁伸手，那一头就不能再依赖它。

### 准入四问（本层唯一的边界判据）

包名叫 `storage`，听起来什么存储相关的事都能进，所以准入必须有判据而不是感觉：

```text
它是「内存模型 ↔ 盘上字节」的转换本身吗？            进层
它是关于这个转换的策略（做不做、重试几次、留多久）？  不进
它是编排（谁调、何时调、排队、并行度）？              不进
它是业务语义（HTTP 状态码、告警阈值、算不算停顿）？    不进
```

一条容易滑过去的区分：**「失败了要不要重试」是策略，不进层；「这次失败是环境坏了还是数据
坏了」是层内事实，层必须能答**（R6）。上层拿不到这一档就没有重试依据。

据此不进本层的东西（清单与 `app/storage/__init__.py` 的 docstring 必须一致）：

| 不进的东西 | 归谁 | 判据 |
|---|---|---|
| 检测结果批缓冲 | `ClientQueues.ca_detections`（cq 上）+ recording 的 detections 队列 | 有状态 / 编排（D4） |
| 事实合并规则（保留谁） | `OfflineRunner._replace_segments` | producer 语义，不是格式事实 |
| run 的分配时机与互斥（谁调 `allocate`、在哪把锁里） | `app/services/run_control/`（`lock_for` 内） | 编排。反过来 run_id 生成 + 建 run 目录本身**进层**（`runs.allocate`）：目录名即身份，是盘上事实 |
| TTL 保留天数 / 扫描周期 / 删不删 | `app/daemons/cleanup/` | 策略（删除动作本身经层内原语 `utils.fs.remove`） |
| 失败要不要重试、重试几次 | 调用方 | 策略 |
| MediaToken / HTTP 状态码 / token 化 URL | `app/routers/` | 业务语义 |
| 异步调度（队列 / WorkerPool / strategy 分发） | 编排方 | 编排 |
| VOD 清单文本 | `app/services/utils/vod_playlist.py` | 装配产物，不是资源元数据（§2） |
| Label Studio 运行时配置 | `settings` 路径字段 + `app/services/lab/runtime_config.py` | 是配置不是产物，**没有 run 身份键** |

反过来，**在层内**且常被误认为不属于这里的：ffmpeg 转码参数 / tfdt hex-patch / timescale
pin、`cv2.VideoWriter` / `eff_fps` 反推、段解码（mp4 → 像素帧）、为编解码起外部工具子进程。
它们全是「转换本身」。留在层外的代价是**静默错**：`eff_fps` 一个值同时决定媒体时长、EXTINF、
tfdt 三处（推导见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)），在层外算就要分三条路
跨边界传，写岔任何一个都不报错。

---

## 1. 分域：一域一个 import 名，产物按同一组域名隔离目录

代码怎么切包，产物就怎么切目录——两件事共用一组域名。

```text
对外按域分，不按读写分
  read.py / write.py  →「盘上有哪些 step」（目录层）与「这个 step 的段有哪些」（HLS 域）
                        挤进同一模块，两个抽象层级
  hls / inference    →  每个函数自带域限定词，hls.* 不可能被要求回答 detections.jsonl
                        在哪，名字本身就在挡膨胀
```

| 域 | 形态 | 管什么 |
|---|---|---|
| `app/storage/hls/` | 子包（facade `__init__` + 实现模块） | 段 / init / playlist / sidecar：定位、编解码、读写、媒体轴、跨度查询 |
| `app/storage/inference/` | 子包（facade + `_layout` / `_jsonl` / `_detection` / `_temporal`） | `detections.jsonl` / `temporal.jsonl` / `label_probs.npz`：定位、编解码、读写、有无离线结果查询 |
| `app/storage/lab.py` | 未落地 | 送标 / 导出临时件 |
| `app/storage/tasks.py` | 单文件，跨域 | 有哪些 task / step：`list_task_ids` / `latest_run_id` / `list_step_ids` |
| `app/storage/runs.py` | 单文件，跨域 | run 目录的分配与查询：`allocate` / `query` / `successor` / `query_latest_by_step` / `query_lifespan_ms` |
| `app/storage/utils/` | 包内私有底座 | `root.py`（白名单 + 定位 + 域目录只建一级）、`fs.py`（盘上原语） |

**子包按产出层切模块，不按产物拆平级域**：`inference/` 的两份产物有两对独立 codec、两条写
路线（B / C），故 `_detection` / `_temporal` 分文件；但它们共用时间轴与行框定，拆成两个平级
域反而要跨域配对。

**薄域单文件、重域子包，对外看不出区别**：`from app.storage import hls, inference` 拿到的都是
一个域的公开面，调用方分不出 `hls` 是包还是模块。所以「薄域将来变重」是**非破坏性升级**
（`inference` 就是从单文件长成子包的）。
代价一条：子包的 `__init__` 是 facade，re-export 会连带加载实现模块，故那些模块的**模块级
必须保持 stdlib + `app.types`**（域货币之外的重依赖如 cv2 走函数体内 import），导入预算才守得住（D1）。

**域读写口一律以 `run: RunIdentity` 开头且只收它**：不收 `(task_id, step_id)` 散标量，传错在
`utils.root.domain_dir` 即 `TypeError`、且不建任何目录。`RunIdentity` 只由 `runs.allocate` /
`runs.query` 产出，于是「写哪一代」「读哪一代」在签名上就是一个值，调用方没有机会拼出一个
不存在的身份。

**跨域模块不出定位能力**：往某个域里写东西是那个域自己的事。`tasks` / `runs` 只在「跨所有域」
时出面，以 `task_id` 或 `(task_id, step_id)` 开头——**本层不提供脱离身份键的产物读写**。

### 成员命名：动词前缀表达「碰盘方式 + 返回粒度」

**动词前缀是封闭集合**，新增成员必须落在其中一个；不确定选哪个说明这个成员的职责还没想清。

```text
list_<复数>       枚举盘上事实 → 列表，一次 iterdir / 一次读
read_<单数>       读一份产物 → 内存对象
iter_<复数>       流式产出，O(1) 内存
query_<问题>      从盘上事实推出一个答案 → 句柄 / 布尔 / 汇总形状（可跨多次读盘）
insert_<单数>     事务式写一份（路线 A）
append_<复数>     追加一批（路线 B）
write_<单数>      整体替换（路线 C）
allocate          分配一个新身份并建出它的目录（全层唯一，`runs.allocate`）
<名词>_path       定位
<名词>_name / parse_<名词>_name    名字的编解码对（互为逆运算）
```

例：`runs.query`（→ `Optional[RunIdentity]`）、`hls.query_span`（→ `HlsSpan`）、
`hls.query_has_segments`（→ bool）、`inference.query_has_offline_results`。`runs.successor` 与
`tasks.latest_run_id` 是集合外的既有名字。

**本层对外不出删除成员**：换代靠一 run 一目录（§6），回收只有 TTL，经包内私有原语
`utils.fs.remove`（原子 rename 进回收区）。`fs.remove` / `fs.purge_trash` 是底座原语，不是域
成员，不受本动词集合约束；它们唯一的包外调用方是 `app.daemons.cleanup`。

三条配套约定：

- **过滤条件进名字，不进 bool 参数**：`list_segments_in_range`，而不是
  `list_segments(start_ts=..., end_ts=...)`。理由见 R4/R5 的取舍——一次调用一次扫盘，过滤
  判据往往与要一起返回的字段同源。
- **返回 id 列表就在名字里说明**：`list_step_ids` 返回 `List[int]`，不叫 `steps`（那读起来
  像返回 step 对象）。
- **同义动词只留一个**：读一份产物一律 `read_`（不用 `load_` / `get_`）；读侧派生答案一律
  `query_`。域内动作可省宾语——`hls.insert_segment` 的域由 import 名给出；跨域模块不能省，
  故是 `tasks.list_step_ids`。

### 路径隔离靠四道执行机制，不是约定

```text
{root}/{task_id}/{step_id}/{run_id}/
  hls/        {track}_segment_{ts_ms}.mp4 / {track}_init.mp4 / {track}_playlist.m3u8
              raw_segment_{ts_ms}.idx / .stage_{track}_{ts_ms}/
  inference/  detections.jsonl / temporal.jsonl / label_probs.npz
{root}/.trash/                     本层回收区
```

**step 下只有 run 目录；run 下只有 `DOMAINS` 域目录、没有文件；存储根下只有数字命名的 task
目录与本层回收区 `.trash/`**——`.lab_exports/` 与 `lab_runtime_config.json` 这类寄居者不符合它。
这是本层对产物落点的要求，层外自己拼路径的写者不受它约束；盘上此刻长什么样见
[ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。

| 机制 | 挡什么 | 不挡会怎样 |
|---|---|---|
| `domain` **必填、无默认** | 漏传 | 文件写回 run 根、退回平铺——谁也说不清哪个文件归谁。现在是 `TypeError` |
| `utils.root.DOMAINS` **白名单** | 打错 | `"feature"` / `"HLS"` 静默造出多余的子目录：写侧不报错、读侧只是"查不到"、TTL 整 step 回收照样删掉，**连残留证据都不留**。现在是 `ValueError` |
| **写者不建父目录** | 回收后的迟到写 | `domain_dir(create=True)` 只建域这一级、`fs.replace` / `fs.ensure_dir` 不带 parents，run 目录只有 `runs.allocate` 建。否则 run 目录被回收后，迟到的写把它重建成只含半截产物的僵尸目录。现在是 `FileNotFoundError` |
| **路径不跨代复用** | ABA | `run_id` 在同 step 内严格递增（`max(now_ms, 已有最大 + 1)`，分配在调用方的串行点内）。否则新一代复用旧路径时，「目录在」不再等于「我那一代还在」，上一条机制就放行了错的写。有了它，「目录在不在」即「这个 run 还在不在」 |

校验发生在 `mkdir` **之前**（`test_unknown_domain_creates_nothing` 钉死），否则错误的域目录
已经落盘了才报错。新增一个域要改 `DOMAINS` 常量——这是有意的：往 run 目录里塞新子目录该是
一次显式决策。

**域名是布局（归本层白名单），产物文件名是内容（归各域自己）**，这条分界决定白名单放在
`app/storage/utils/root.py`、文件名放在各域。

**目录名能当时间用，就别读 mtime**：分层之后，写产物只更新最深一级目录的 mtime，上层目录纹丝
不动（只在增删直接子项时变）。故「最近活动」取最大 `run_id`（目录名即开跑时刻，
`tasks.latest_run_id`），不读 mtime、不下钻；而 `{step}/` 的直接子项只有 run 目录，它的 mtime
恰是「最近一次分配」，TTL 判据用的正是它——两个口径不能复用同一个函数。

---

## 2. 域容器：`types.py` 集中声明本域收发的形状

`app/storage/hls/types.py` 已落地（`SegmentRef` / `Segment` / `HlsSpan`）。形状该放哪，三问：

| 问 | 归属 | 例子 |
|---|---|---|
| 换掉落盘格式，它会不会跟着消失？ | 会 → **域 `types.py`** | `SegmentRef` 就是文件名解出来的；`Segment` 的身份与时长都来自清单同一行 |
| 不会，且是全仓通用的内存契约 | `app/types/` | `Frame` / `FrameDetection` / `TemporalSegment` / `LabelProbs` / `RunIdentity` |
| 它是给别人消费的**装配产物**，不是资源的元数据 | 出域 | `VodEntry` → `app/services/utils/vod_playlist.py` |

判据**不在「跨不跨服务」上**：让 `app.types` 认识 `SegmentRef`，等于让最上游的契约层持有
最下游的文件名格式知识。`app.types` 要挡的是「服务之间为了拿契约而互相依赖、顺带传染重
依赖」，而调用方 import 本层不带任何重依赖（hls 域重依赖集合为空）。

四条执行细则：

1. **`types.py` 是子包的底**：stdlib only，且**不 import 同包任何模块**——`_layout` /
   `_m3u8` / `_read` / `_write` / `_decode` 都往这里取形状。轨道白名单 `TRACKS` 与校验器
   `require_track` 因此留在 `_layout`：那是「布局与命名」的词汇表，不是形状；搬进来会让
   `types` 反向依赖 `_layout`，底就不是底了。
2. **只放对外契约**。函数内部传参用的四行 NamedTuple 放在用它的地方旁边。
3. **准入判据 2 同样适用**：「有几个包会因为它变了而出错？< 2 不进」。反例：`Step` 摘要建了
   又撤，因为只有 `app/routers/task.py` 一个消费方。正例：`HlsSpan`（段跨度）有 task /
   traceback / lab 三个消费方，于是进来，段跨度的算法也因此只剩一份。
   没有「从文件名解出的身份」这类形状的域就不建 `types.py`（`inference/` 的货币全在 `app.types`）。
4. **同名不同义要避开**：`step` 在本仓已是业务实体（`clean_task.current_step`、
   `clean_alarm.step_id`），域内不再定义同名类型。

**读侧形状按「≥2 个消费方」准入，不按档数卡**：hls 域现出四档——① 段容器（`SegmentRef` /
`Segment`），② `Frame`，③ 跨度 `HlsSpan`，④ 媒体轴 `MediaTimeline` / `PlacedSegment`（ai /
traceback / lab / clip_builder 四个消费方）。新形状要进来，先答「它是资源的元数据，还是给别人
消费的装配产物」，再数消费方。

---

## 3. 定位（L）：路径集中在层内，业务层一个字符串都不拼

```text
查询 / 外部输入 ──parse_*──▶ 结构 ──定位──▶ Path ──▶ 外部工具读写
                              ↑ 校验只在这一步
```

| 编号 | 规则 | 不这么做会怎样 |
|---|---|---|
| L1 | **定位函数收结构，不收散标量**：`segment_path(run, ref)` | 读写两侧各拼一次文件名，迟早分叉 |
| L2 | **外部输入先经 `parse_*` 转结构，路径由结构重建** | path traversal 从「靠校验挡」变成结构上不可能。`endswith("init.mp4")` 这类顶替判据会放行 `evil_init.mp4`（`app/routers/media.py` 走 `parse_init_name`） |
| L3 | `SegmentRef` **带 `track`、不带 `task_id` / `step_id` / `run_id`** | track 在文件名里、`parse_segment_name` 必须解出；身份键由调用方以 `RunIdentity` 持有 |
| L4 | **域根一律经 `utils.root.domain_dir(run, domain)`**，域名在一个域文件里只出现一次 | 域名散落之后，白名单救不了拼错的那一处 |
| L5 | **域文件不得自己读 settings、不得自己拼根** | 根解析只有 `utils.root._storage_root()` 一处 |

**定位与建目录分开**（`app/storage/utils/root.py`）：

```python
path(task_id=None, step_id=None) -> Path                      # 逐级定位，不建目录
run_path(run, domain=None) -> Path                            # {task}/{step}/{run_id}[/{domain}]，不建目录
domain_dir(run, domain, *, create=False) -> Path              # 域文件统一入口；create 只建域这一级
```

`path` 参数从左到右逐级下钻，省到哪一级返回哪一级；跳级（给了 step 省了 task）抛
`ValueError`——那会静默少一层目录、产物落到上一级。用多个名字表达同一条路径的前缀迟早长出
新的组合。

**`create` 只建最后一级，理由是回收**：写者建父目录 = run 目录被回收后，迟到的写把它重建成
僵尸目录。产物目录（run 目录）只由分配者 `runs.allocate` 建；域写口 `create=True` 只经
`fs.ensure_dir` 建域这一级（不带 parents），run 不在即 `FileNotFoundError`。`domain_dir` 对非
`RunIdentity` 抛 `TypeError`、对非白名单域名抛 `ValueError`，都早于建目录。

**删除只在 `utils/fs.py`**（经回收区原子 rename，§6），任何域文件与 `tasks` / `runs` 都不删。
**枚举**：`root.run_ids` 是底座里唯一的枚举（run 目录 id 升序，供 `runs` / `tasks` 共用）；
task / step 枚举在 `tasks`。

**`utils/root.py` 与 `utils/fs.py` 包内私有**：交出 `root` 就等于交出「根 + 两级 id + run + 域」
的拼装能力，settings 那道门禁就白设了。`fs` 唯一的包外调用方是 `app.daemons.cleanup`（只用
`remove` / `purge_trash`）。

**各域文件的样板**（写死在 `utils/root.py` 的 docstring 里，照抄）：

```python
from app.storage.utils import root as _root

_DOMAIN = "hls"


def domain_dir(run: RunIdentity, *, create: bool = False) -> Path:
    return _root.domain_dir(run, _DOMAIN, create=create)
```

> ⚠ **别把 helper 命名成 `_root`**：同名函数会遮掉模块别名，`_root.domain_dir` 立刻
> `AttributeError`。调用处统一别名 `_root` / `_fs`，也是为了不与形参 `root=` 撞名。

**根解析（`utils.root._storage_root()`）三条硬约束**：

- **必须记忆化**：`settings.storage_base_dir` 每次访问都跑 `.resolve()`，那是 syscall，而
  定位函数每段落盘、每次请求各调若干次。
- **key 取 `settings.storage_dir` 原始字符串**：`tests/conftest.py` 的 `tmp_storage` fixture
  靠 `monkeypatch.setattr(settings, "storage_dir", ...)` 把读写两侧一起指到临时目录。
- **不能写成模块级常量**：那在 import 期定死，patch 完全失效，失效的表现是**测试去写真实
  `database/`**。

**路径出层、根不出层**（设计约束 3）：不出路径就得把每个用到路径的动作（`FileResponse`、
送标导出）都搬进来，那正是上帝类的长法。层只出模块函数，不出句柄：调用方不构造任何「带根
的查找器」对象。

---

## 4. 读侧（R）

| 编号 | 规则 | 备注 |
|---|---|---|
| R1 | **出结构，不出字节、不出句柄** | 返回 dataclass / NamedTuple / ndarray / 基本类型；不出 `IO`、不出裸 `bytes` |
| R2 | **不出目录、不出存储根**；载荷字节的取用位置照出 `Path` | 出目录 = 把布局复制给调用方，且门禁抓不到（`dir / "x"` 是普通拼接） |
| R3 | **只解析本域格式，不做业务判断，不出领域异常** | 阈值、该不该删、算不算停顿、映射成什么状态码，一律留调用方。多态失败用**事实枚举 + NamedTuple**（`Vod(state, text, segments)`），不是 `Optional[str]` |
| R4 | **落盘状态是事实，可做过滤参数**；选错会静默出错的参数**必填、无默认** | `list_task_ids(order=)` 可给默认（选错只影响清单顺序）；`read_segment` 的 `width`/`height` 不给默认（下游会变成 train-serve skew） |
| R5 | **一次调用一次扫盘** | 按「一次调用 = 一件事」切函数；不照搬取值器，也不合成跨口径黑盒 |
| R6 | **环境坏了原样抛，内容坏了逐行隔离** | 打不开 / 写不进 / 建不了目录 → `OSError` 原样抛；单条记录解析不出 → 跳过 + warning，不让一行毁掉整个文件 |

**R4 与 R5 打架时，切函数优先**：hls 读侧曾设计成 `list_segments(..., playable_only=)` 必填
bool，改成了两个函数——过滤判据与段时长同源于清单键集合，bool 参数方案下调用方拿到段之后
还要再读一次 playlist。

**但一个域里「有哪些 X」最终只许有一个入口**：上面那两个函数中枚举文件系统的那一个（盘上有
哪些段）后来被**整个删除**，不是降级成私有——`__all__` 拦不住包内误用，留着就是第二个真源。
收口后只剩 `list_segments`（读清单，带 EXTINF）与它的区间切片 `list_segments_in_range`，两者
同源、同返回类型。删掉之后「忘了滤」这个风险随之消失，R4/R5 的取舍也不再需要靠 docstring +
review 兜底——**能删掉一个口径，就别留着靠约定**。

**字节的转换归谁做**：

| 情形 | 归谁 |
|---|---|
| 只转发字节（`/media/*` 送 mp4 给浏览器） | 层出 `Path` |
| 有结构的编解码（`.idx` / playlist / `detections.jsonl` / `temporal.jsonl` / `label_probs.npz` / fMP4 fragment） | **层内转**，含为此起外部编码器（D3） |
| 解码（段 mp4 → 像素帧） | **层内转**，`read_segment` / `iter_frames` 与 `insert_segment` 互为逆运算 |

---

## 5. 写侧（W）：三条路线

**调用方一律只交内存对象、只调一次函数**——路线是层内实现，不出现在签名上。对外形态就是
`INSERT`：给一行数据，剩下的是存储的事。

```text
路线 A  transaction（外部工具产出 / 有位置依赖）
    ① stage    锁外，层内起编码器在 {run}/{domain}/.stage_{产物键}/ 造产物
    ② adjust   读既有状态、做位置相关的最后修补（无位置依赖则 ①→③）
    ③ commit   原子 rename + 登记
  任一步异常 → 删 stage、不 rename、不登记，原异常上抛

路线 B  append（追加）
    收内存对象 → 序列化 → 一次 open(mode="a") + write

路线 C  overwrite（层内纯序列化，整体替换）
    ① 编码     整批先编码完（失败时盘上一个字节没动）
    ② 写 tmp   与目标同目录的 .{name}.tmp
    ③ rename   os.replace 原子换名
  全层唯一实现 utils.fs.replace(path, write_fn)：不建父目录；任何异常先删 tmp 再原样上抛
```

**A 与 C 的分界**：产物字节由**外部工具**造、或有位置依赖 → A；层内从内存对象**纯序列化**
→ C。

| 编号 | 规则 | 路线 |
|---|---|---|
| W1 | **stage / tmp 必须与目标同卷**（落在 `{run}/{domain}/` 之下），不得用 `tempfile.mkdtemp()` | A C |
| W2 | **目标文件名在 rename 前不得存在**——名字一出现就是合法产物 | A C |
| W3 | **位置相关的修补必须在 rename 之前** | A |
| W4 | **失败即整体作废**：删 stage/tmp、不 rename、不登记，原异常上抛 | A C |
| W5 | **一次调用写完调用方给的一批，层内不攒批** | B |
| W7 | **stage 目录名取「与产物同键」，不用随机 nonce** | A |
| W8 | **登记顺序：索引先于主产物可见，主产物先于清单条目** | A |

> W6 不存在（原条文已删，编号不复用——别的文档还在引 W1–W5）。

**W7 的取舍**（`.stage_{track}_{ts_ms}` 而不是 `.stage_{nonce}`）：同键 = 同产物名，撞键在
`os.replace` 那步本就是既有 bug，不引入新的；而随机 nonce 每次崩溃都留一个新目录无人回收，
同键则**重试自然复用**（入口 `rmtree` 一次即幂等），目录名还自带「哪一份产物没写完」。

**W8 展开**（写错三条都不报错或错在下游）：索引类（sidecar）必须先于主产物可见——产物名一
出现就对读侧可见，反过来会留下「主产物可见但索引未就位」的窗口；主产物就位后才 append 清单
条目——清单有行无文件 = 播放器直接报错；清单头（含 `EXT-X-MAP`）必须先于任何条目行。

**每类产物走哪条**：

| 产物 | 路线 | 理由 |
|---|---|---|
| `{track}_segment_*.mp4` / `{track}_init.mp4` | **A** | 外部编码器产出、字节量大；且有位置依赖（tfdt = 前面所有 EXTINF 之和） |
| `{track}_playlist.m3u8` 的 `#EXTINF` 行 | **B** | 一行文本，作为 A 的 ③ 登记步执行 |
| `detections.jsonl` | **B** | 追加型，没有"先造好一个完整产物"这回事 |
| `temporal.jsonl` | **C** | 多写者共居、整批替换，内容由层内序列化；合并（保留谁）归调用方 read → merge → write |
| `label_probs.npz` | **C** | 二进制整体替换，`np.savez` 写文件对象（避免自动补后缀） |
| `raw_segment_*.idx` | **C** | 层内纯序列化（float64 数组），经 `fs.replace` 整体替换；作为 A 的 ③ commit 第一步执行（W8：索引先于主产物可见） |

---

## 6. 并发：本层不持锁

**顺序是构造出来的，不是抢出来的。**

```text
同一 run 内的顺序   由调用侧的串行点构造（SerialTaskQueue 提交序 / 单个离线进程）
跨 run 的隔离       由盘上路径不跨代复用构造：一 run 一目录，写者只写自己 RunIdentity 指向的目录
run 的分配          由调用方的串行点构造（allocate 在 lock_for 内，run_id 才严格递增）
回收                只有 TTL 整 step，经 utils.fs.remove 原子 rename；写者不建目录，迟到写原子失败
```

**回收与写侧 / 读侧无需互斥**：换代不删任何东西，旧一代迟到的写落进它自己的 run 目录；回收
时 run 目录整体 rename 进回收区，迟到写在 `domain_dir` / `fs.replace` 处 `FileNotFoundError`，
锁定该 run 的读者拿到「不存在」。没有任何一步需要写与删同序。详见
[DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md) §3.1 与
[DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md)。

**为什么不是层内加锁**：锁保证的是「不重叠」，而换代需要的是「旧写者已终止」——一个还在写
的写者，锁只能让删除等一下，等完了它继续写，僵尸目录照样出现。**互斥挡不住一个没停的写者**；
能挡住它的是不删它的目录（分目录）和不让它建目录（写者不建父目录）。

代价如实记账，两条：

- 这条保证**不在层内、门禁抓不到**。各域写入口的 docstring 必须写明它依赖这个前提。
- **队列不能加 worker**。加了不报错，表现是同一 run 内 tfdt 碰撞（后段覆盖前段、画面丢一截）。
  配置里不给这个旋钮 + `SerialTaskQueue` 类名 + 注释共同守着。

---

## 7. 依赖与状态（D）

| 编号 | 规则 |
|---|---|
| D1 | **模块级默认 stdlib-only**，重依赖走 D2。唯一例外是**域货币**（该域每个签名都在收发的类型，如 `hls` 的 `Frame`、`hls._idx` 的 float64 数组、`inference._temporal` 的 `LabelProbs`，随之带进 numpy），它没法推迟到函数体内，必须在 `tests/test_import_hygiene.py` 的 `BUDGET` 里逐模块登记上限。不是货币的重依赖（cv2）一律走 D2；`FrameDetection` 是纯 stdlib，故 `inference._detection` 不带 numpy |
| D2 | 单个重函数要的依赖走**函数体内 import**，别让整个域为它变重 |
| D3 | **编解码可起外部工具子进程**，三个条件：① 必须有超时；② 失败即整体作废（W4），不留半成品；③ 只用于编解码——调度、编排、并行度一律不进层 |
| D4 | **可持有**解析缓存；**不可持有**批缓冲、待写内容、owner fence、连接、任何要 `start()/stop()` 的东西。**不出句柄** |
| D5 | **外部工具的可用性是运行时依赖，不是 import 期依赖**：`import app.storage.hls` 不得要求机器上有 ffmpeg；缺二进制在调用 `insert_segment` 时才炸，读侧不受影响 |

---

## 8. 测试（T）

| 编号 | 规则 |
|---|---|
| T1 | **每个 codec 必须有往返测试**——这是转换契约唯一能被验证的地方。写进去的内存对象读回来要相等；层是双向的，往返链条一直闭合到「段」这一档 |
| T2 | `ts_to_ms` 的往返断言在 **ms 域**闭合：`parse(name(ts)).ts_ms == floor(Fraction(ts) * 1000)`，且 `Fraction(ms, 1000) <= Fraction(ts) < Fraction(ms + 1, 1000)`；**不是** `== ts`，也**不能**用 `int(ts * 1000)` 作期望值（乘法先舍入：`0.29 * 1000 == 290.0` 而 0.29 精确值略小）。向下取整有损，读侧段级定位的 `bisect_right - 1` 正建立在「段名 ≤ 首帧」上 |
| T3 | **事务不变式必须能脱离外部工具测**：保留私有的 commit 入口，喂手工造的最小合法产物，断言 W2 / W4 / W8。否则最该测的断言躲在需要 ffmpeg 的函数背后，CI 缺二进制时静默失效 |
| T4 | **需要外部二进制的测试单独分档**（marker）。层的主体测试必须毫秒级、无外部依赖——它是所有人的下游，跑得慢就没人跑 |
| T5 | **并发有一条真测试**：多线程同时写同一冲突键，断言产物齐备、清单行数正确、位置相关字段（如 tfdt）不碰撞。测试碰私有面在 T3/T5 是正当的——被测的是内部不变式，不是公开契约 |

现有覆盖见 `tests/test_storage_{tasks,runs,fs,hls,inference,cleanup_ttl}.py` 与
[TESTING_MAP.md](TESTING_MAP.md)。

---

## 9. 设计约束与门禁

### 9.1 新增成员前过一遍（编号被代码引用）

1. **一域一个 import 名**，函数带域限定词。域内怎么拆文件是域自己的事。
2. **准入判据**：这个知识有几个包会因为它变了而出错？< 2 → 不进。
3. **路径出层，根不出层。**
4. **不出句柄，只出模块函数。** 层可以持有层内状态，但不交出去。
5. **能用参数解决的不拆方法**；但**涉及正确性的参数不给默认值**——漏传是 `TypeError`，不是
   静默走错分支。
6. **朴素**：只回答格式事实，不做业务判断、不定义错误语义。
7. **能力太窄的不包装成接口**：调用方自己一行就能写对的动作，包装它只是多一层。
8. **并发由调用侧的串行调度保证，本层不持锁**（§6）。

### 9.2 门禁映射

| 规则 | 可执行性 |
|---|---|
| L4 域名白名单、`domain` 必填 | ✅ 已有 |
| 依赖白名单（只许 `app.storage` / `app.types` / `app.settings`） | ✅ 已有（白名单式，反向探针验过） |
| D1 模块级 stdlib-only | ✅ 逐模块 `BUDGET` + 覆盖检查，新域文件漏登记就红 |
| T1 / T2 往返测试 | ✅ 每个 codec 一条，写不出来说明正反运算没同居 |
| T3 事务不变式 / T5 并发 | ✅ T5 是概率性的，但没有串行保证时必红 |
| L2 外部输入经 `parse_*` | ✅ `parse_segment_name` / `parse_init_name` 对 `../`、绝对路径、非法 track、非数字 ts 返回 `None`；`segment_path` 入参不收裸 `str` |
| R4 正确性参数必填 | ✅ 漏传 `TypeError` |
| D3 子进程必须带超时 | ✅ AST 扫 `subprocess.*` 调用有无 `timeout=` |
| L5 `settings.storage_base_dir` 访问面收敛 | ⏳ 未加。层外仍有直读它的地方（lab 临时件根与运行时配置、cleanup daemon 的扫描根），现在加会立刻红 |
| 文件名字面量不出层 | ⏳ 未加。**AST 扫 `ast.Constant` 并跳过 docstring**，不能 grep（`_segment_` 会命中 30 多处 `ca_segment_len`） |
| R1 / R3 | ⚠️ 靠返回类型标注 + review |
| D3 的另两个条件（失败作废、只用于编解码） | ⚠️ review |
| **W 的路线选择（A / B / C）** | ❌ **不可测，只能 review**。这是本规范最需要人看的一条：走错不会红，表现是「中间态被读到」，而那正是静默失败。新增域文件的 PR 必须在描述里写明每类产物选了哪条路线及理由 |

---

## 10. 旧编号对照（代码里还写着 `§7.x`）

规范正文原先是 `docs/update/20260909_STORAGE_LAYER_BASE.md` 的 §7，已迁到本文并删除原文。
代码 docstring 与测试里残留的引用按下表读：

| 代码里写的 | 现在在哪 |
|---|---|
| 规范 §7.0（准入四问） | 本文 §0 |
| 规范 §7.1 R1–R6 | 本文 §4 |
| 规范 §7.2 路线 A / B / C、W1–W8 | 本文 §5 |
| 规范 §7.3 L1–L5 | 本文 §3 |
| 规范 §7.4 C1–C7（层内锁） | **已作废**，改为 §6 零锁 |
| 规范 §7.5 D1–D5 | 本文 §7 |
| 规范 §7.6 T1–T5 | 本文 §8 |
| 规范 §7.7 门禁映射 | 本文 §9.2 |
| 规范 §7.8（解码进不进层） | **已决**：进层，见 §4 末表 |
| 设计约束 1–8 | 本文 §9.1 |

**已作废、不要再照着实现的三条**（写在这里只为挡住回潮，不展开）：

- **层内 per-`(task, step)` 锁 / `_locks.py`**：模块从未接线即删除，改为 §6 的串行队列。
- **「storage 只管名字与定位，内容怎么读写不进层」**：编解码是本层本职（§0）。
- **删除与各域写者靠读写锁互斥**：该方案已废。§6 现为：回收与写者不互斥（写者不建目录 + 原子删除）。

## 代码来源

- `app/storage/__init__.py`（边界清单与设计约束，与本文 §0 / §9.1 必须一致）
- `app/storage/utils/root.py`（`DOMAINS` 白名单、`path` / `run_path` / `domain_dir` 只建一级、`run_ids`、根解析记忆化）
- `app/storage/utils/fs.py`（路线 C 唯一实现 `replace`、原子删除 `remove` / `purge_trash`、`ensure_dir`）
- `app/storage/tasks.py`（`list_task_ids` / `latest_run_id` / `list_step_ids`）
- `app/storage/runs.py`（`allocate` / `query` / `successor` / `query_latest_by_step` / `query_lifespan_ms`）
- `app/storage/hls/`（`types` / `_layout` / `_encode` / `_decode` / `_fmp4` / `_m3u8` /
  `_idx` / `_write` / `_read` / `_timeline`）
- `app/storage/inference/`（`_layout` / `_jsonl` / `_detection` / `_temporal`）
- `app/types/run.py`（`RunIdentity`）
- `app/services/utils/task_queue.py`（`SerialTaskQueue`，§6 的串行保证）
- `tests/test_import_hygiene.py`（依赖白名单 + 导入预算门禁）
- `tests/test_storage_{tasks,runs,fs,hls,inference,cleanup_ttl}.py`
- 变更记录：`docs/update/20260909_STORAGE_LAYER_BASE.md`（第 1 期）、
  `20260911_STORAGE_HLS_DOMAIN.md`、`20260912_HLS_READ_CAPABILITY.md`、
  `20260921_STORAGE_INFERENCE_DOMAIN.md`、`20260927_STORAGE_RUN_DIR_PROPOSAL.md`（run 目录设计推导）、
  `20260927_STORAGE_FS_PRIMITIVES.md`、`20260927_STORAGE_RUN_IDENTITY.md`、
  `20260928_STORAGE_QUERY_ADD.md`、`20260928_STORAGE_UTILS.md`、`20260928_TIME_UNIT_MS.md`
