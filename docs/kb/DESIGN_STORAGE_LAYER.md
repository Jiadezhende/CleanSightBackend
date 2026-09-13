> 更新时间：2026-09-13
> 依据来源：代码分析（`app/storage/` 全量 + `tests/test_storage_*.py`）
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据层 `app/storage/` 设计规范

**本文是准入判据，不是现状描述。** 新增域文件、新增成员、review 存储层 PR 时按它判。

> **适用范围与当前状态**：`app/storage/` 的代码已全部落地并有单测覆盖，但**生产调用点一个
> 都没迁**——现役落盘布局仍是 `{task}/{step}/` 平铺（见
> [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)），本文描述的
> `{step}/{domain}/` 域隔离布局在迁移完成那一刻才生效。**迁移进度不在知识库维护**（它是
> 过程状态，随迁移完成消失）：各期做了什么见 `docs/update/` 里的 `*_STORAGE_*` 系列记录。
>
> **条文编号是代码引用的一部分**：R/W/L/D/T、路线 A/B/C、设计约束 1–8 被 12 处 docstring 与
> 测试直接引用，**改条文可以，改编号要先搜引用**。代码里残留的 `§7.x` 写法见 §10 对照。

---

## 0. 一张图：层在哪、经手什么

```text
调用方（services / routers）
   │  交内存对象 + (task_id, step_id)               拿内存对象 / 已定位的 Path
   ▼                                                        ▲
┌──────────────────────────── app/storage/ ────────────────────────────┐
│  hls/        feature.py     lab.py(未建)     tasks.py                │  §1 分域
│   types.py    (FrameFeature)                 (跨域枚举 / 删除)       │  §2 域容器
│   _write / _read / _encode / _decode / _m3u8 / _fmp4 / _idx / _meta  │  §4 §5 转换
│   _layout.domain_dir ──┐                                             │
│   feature._domain_root ┴──▶ _root.path(task, step, domain, create)   │  §3 定位
└──────────────────────────────────────────────────────────────────────┘
   ▼
{root}/{task_id}/{step_id}/{hls|features|lab}/    域名过白名单，step 根下无文件
```

**契约一句话**：storage 是**内存数据模型与盘上数据之间的抽象**。调用方交出内存对象、拿回
内存对象或已定位的 `Path`；文件名、目录布局、文本格式、二进制布局、编解码全在层内，上层
不碰其中任何一样。层是**双向**的——写进去 `Sequence[Frame]`，读出来是 `Frame`。

**位置**：`app/storage/`，与 `app/services/` 平级、在它下面一层。它不是服务，是数据层；
放进 `services/` 里，「它不许依赖任何服务」就成了需要写 8 行解释的例外。`app/domain/` 是
这个位置的现成先例。

**依赖白名单**：stdlib、三方，以及 `app.domain`（内存契约）与 `app.settings`（落盘根的唯一
来源，只在函数体内 import）。别的 `app.*` 一律不行——包括 `app.database` / `app.models`：
它们进来不造环、不报错，只会把数据层绑死在 ORM 上。门禁
`tests/test_import_hygiene.py::test_layer_package_imports_only_whitelisted_app_modules`。
这条是写侧 persistence 与读侧 traceback / lab / inference.offline / routers 能**同时向下
依赖它**的前提——一旦它向上或向旁伸手，那一头就不能再依赖它。

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
| `FeatureStore` 批缓冲 / owner fence / `open_fresh` | `app/services/inference/feature/store.py` | 有状态（D4） |
| TTL 保留天数 / 扫描周期 / 删不删 | `app/services/persistence/workers/` | 策略 |
| 失败要不要重试、重试几次 | 调用方 | 策略 |
| MediaToken / HTTP 状态码 / token 化 URL | `app/routers/` | 业务语义 |
| 异步调度（队列 / WorkerPool / strategy 分发） | 编排方 | 编排 |
| VOD 清单文本 | `app/services/utils/vod_playlist.py` | 装配产物，不是资源元数据（§2） |
| Label Studio 运行时配置 | `settings` 路径字段 + `app/services/lab/config.py` | 是配置不是产物，**没有 `(task, step)` 身份键** |
| `FrameTracker.find` 的位级 ts 匹配 | `app/services/inference/offline/` | 「ts 是帧的身份，配错帧比报错更坏」是业务语义 |

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
  hls / feature / lab →  每个函数自带域限定词，hls.* 不可能被要求回答 features.jsonl
                        在哪，名字本身就在挡膨胀
```

| 域 | 形态 | 管什么 |
|---|---|---|
| `app/storage/hls/` | 子包（facade `__init__` + 实现模块） | 段 / init / playlist / sidecar / metadata：定位、编解码、读写 |
| `app/storage/feature.py` | 单文件 | `features.jsonl`（`facts.jsonl` 的货币未定，整个留在 `inference/feature/store.py`） |
| `app/storage/lab.py` | 未落地 | 送标 / 导出临时件 |
| `app/storage/tasks.py` | 单文件，**唯一的跨域模块** | `steps` / `ids` / `purge_step`——共同特征是「跨所有域」 |

**薄域单文件、重域子包，对外看不出区别**：`from app.storage import hls, feature` 拿到的都是
一个域的公开面，调用方分不出 `hls` 是包还是模块。所以「薄域将来变重」是**非破坏性升级**。
代价一条：子包的 `__init__` 是 facade，re-export 会连带加载实现模块，故那些模块的**模块级
必须 stdlib-only**（cv2 走函数体内 import），导入预算才守得住（D1）。

**`tasks.py` 不出定位能力**：往某个域里写东西是那个域自己的事。它只在「跨所有域」时出面，
且每个入口都以 `(task_id, step_id)` 开头——**本层不提供任何脱离身份键的能力**。

### 路径隔离靠两道执行机制，不是约定

```text
{root}/{task_id}/{step_id}/
  hls/       {track}_segment_{ts_us}.mp4 / {track}_init.mp4
             {track}_playlist.m3u8 / raw_segment_{ts_us}.idx / metadata.json
             .stage_{track}_{ts_us}/   写事务暂存，commit 后即删
  features/  features.jsonl / facts.jsonl
  lab/       送标 clip 与整段导出的临时件（用完即删，残留随 step TTL 回收）
```

**step 根下只有域目录、没有文件；存储根下只有数字命名的 task 目录**（`.lab_exports/` 与
`lab_runtime_config.json` 两个寄居者随之出局）。

| 机制 | 挡什么 | 不挡会怎样 |
|---|---|---|
| `domain` **必填、无默认** | 漏传 | 文件写回 step 根、退回平铺——谁也说不清哪个文件归谁。现在是 `TypeError` |
| `_root.DOMAINS` **白名单** | 打错 | `"feature"` / `"HLS"` 静默造出第四个子目录：写侧不报错、读侧只是"查不到"、`purge_step` 照样删掉，**连残留证据都不留**。现在是 `ValueError` |

校验发生在 `mkdir` **之前**（`test_unknown_domain_creates_nothing` 钉死），否则错误的域目录
已经落盘了才报错。新增一个域要改 `DOMAINS` 常量——这是有意的：往 step 目录里塞新子目录该是
一次显式决策。

**域名是布局（归本层白名单），产物文件名是内容（归各域自己）**，这条分界决定白名单放在
`app/storage/_root.py`、文件名放在各域。

**连带影响，不处理就静默退化**：产物落进 `{step}/{domain}/` 之后，写一个段只更新 `hls/` 的
mtime，`{step}/` 本身纹丝不动（它只在新建域目录那一刻变）。故 `tasks.ids(order="mtime")`
必须下钻域子目录取最大值，否则「最近活动」退化成「首次落盘」。`{step}/` 自身的 mtime 反而
成了「创建时间」的好代理，TTL 判据用的正是它——两个口径不能复用同一个函数。

---

## 2. 域容器：`types.py` 集中声明本域收发的形状

`app/storage/hls/types.py` 已落地（`SegmentRef` / `PlayableSegment`）。形状该放哪，三问：

| 问 | 归属 | 例子 |
|---|---|---|
| 换掉落盘格式，它会不会跟着消失？ | 会 → **域 `types.py`** | `SegmentRef` 就是文件名解出来的；`PlayableSegment` 的"已登记"来自清单键集合 |
| 不会，且是全仓通用的内存契约 | `app/domain/` | `Frame` / `FrameFeature` |
| 它是给别人消费的**装配产物**，不是资源的元数据 | 出域 | `VodEntry` → `app/services/utils/vod_playlist.py` |

判据**不在「跨不跨服务」上**：让 `app.domain` 认识 `SegmentRef`，等于让最上游的契约层持有
最下游的文件名格式知识。`app.domain` 要挡的是「服务之间为了拿契约而互相依赖、顺带传染重
依赖」，而调用方 import 本层不带任何重依赖（hls 域重依赖集合为空）。

四条执行细则：

1. **`types.py` 是子包的底**：stdlib only，且**不 import 同包任何模块**——`_layout` /
   `_m3u8` / `_read` / `_write` / `_decode` 都往这里取形状。轨道白名单 `TRACKS` 与校验器
   `require_track` 因此留在 `_layout`：那是「布局与命名」的词汇表，不是形状；搬进来会让
   `types` 反向依赖 `_layout`，底就不是底了。
2. **只放对外契约**。函数内部传参用的四行 NamedTuple 放在用它的地方旁边。
3. **准入判据 2 同样适用**：「有几个包会因为它变了而出错？< 2 不进」。`Step` 摘要建了又撤，
   因为只有 `app/routers/task.py` 一个消费方，它拿 `list_segments_by_track` 自己统计即可。
4. **同名不同义要避开**：`step` 在本仓已是业务实体（`clean_task.current_step`、
   `clean_alarm.step_id`），域内不再定义同名类型。

**读侧产出的档数要卡住**：hls 域只出两种——① 段容器（`SegmentRef` / `PlayableSegment`），
② `Frame`。第三种形状要进来，先答「它是资源的元数据，还是给别人消费的装配产物」。

---

## 3. 定位（L）：路径集中在层内，业务层一个字符串都不拼

```text
查询 / 外部输入 ──parse_*──▶ 结构 ──定位──▶ Path ──▶ 外部工具读写
                              ↑ 校验只在这一步
```

| 编号 | 规则 | 不这么做会怎样 |
|---|---|---|
| L1 | **定位函数收结构，不收散标量**：`segment_path(t, s, ref)` | 读写两侧各拼一次文件名（现存分叉：写侧从 ts 拼 `.idx` 名、读侧 `with_suffix(".idx")`） |
| L2 | **外部输入先经 `parse_*` 转结构，路径由结构重建** | path traversal 从「靠校验挡」变成结构上不可能。现状 `app/routers/media.py` 用 `endswith("init.mp4")` 顶替判据，放行 `evil_init.mp4` |
| L3 | `SegmentRef` **带 `track`、不带 `task_id` / `step_id`** | track 在文件名里、`parse_segment_name` 必须解出；两个 id 是路由键，调用方手里本来就有 |
| L4 | **域根一律经 `_root.path()`**，域名在一个域文件里只出现一次 | 域名散落之后，白名单救不了拼错的那一处 |
| L5 | **域文件不得自己读 settings、不得自己拼根** | 根解析只有 `_root._storage_root()` 一处 |

**逐级下钻一个入口**，而不是 `root` / `task_dir` / `step_dir` / `domain_dir` 四个名字：

```python
_root.path(task_id=None, step_id=None, domain=None, *, create=False) -> Path
```

它们是同一条路径的四个前缀，参数从左到右逐级下钻，省到哪一级就返回哪一级；跳级（给了深层
省了浅层）抛 `ValueError`——那会静默少一层目录、产物落到上一级。用四个名字表达迟早长出第
五种组合。`create` 放在这里是因为「定位」与「确保能往这写」是同一件事的两半，放各域文件里
就是三份重复的 `mkdir(parents=True, exist_ok=True)`；默认 `False`，不主动碰盘。

**枚举与删除刻意不在 `_root`**：`iterdir` / `stat` / `rmtree` 是 `tasks` 域的能力，混进底座
则各域文件 import 它时会顺带吃一层与自己无关的 IO 语义。

**`_root` 包内私有**：交出去就等于交出「根 + 两级 id + 域」的拼装能力，settings 那道门禁就
白设了（拦属性访问拦不住 `from ..._root import path`）。

**各域文件的样板**（写死在 `_root.py` 的 docstring 里，照抄）：

```python
_DOMAIN = "hls"

# 本域在该 step 下的根目录 —— 域内所有路径函数都经它
def _domain_root(task_id, step_id, *, create=False) -> Path:
    return _root.path(task_id, step_id, _DOMAIN, create=create)
```

> ⚠ **别把 helper 命名成 `_root`**：域文件顶部有 `from app.storage import _root`，同名函数
> 会遮掉模块名，`_root.path` 立刻 `AttributeError`。用 `_domain_root`；重域拆子包时去掉前导
> 下划线叫 `domain_dir`（包内公开、facade 不 re-export，仍不出包）。

**根解析（`_root._storage_root()`）三条硬约束**：

- **必须记忆化**：`settings.storage_base_dir` 每次访问都跑 `.resolve()`，那是 syscall，而
  定位函数每段落盘、每次请求各调若干次。
- **key 取 `settings.storage_dir` 原始字符串**：`tests/conftest.py` 的 `tmp_storage` fixture
  靠 `monkeypatch.setattr(settings, "storage_dir", ...)` 把读写两侧一起指到临时目录。
- **不能写成模块级常量**：那在 import 期定死，patch 完全失效，失效的表现是**测试去写真实
  `database/`**。

**路径出层、根不出层**（设计约束 3）：不出路径就得把每个用到路径的动作（`FileResponse`、
送标导出）都搬进来，那正是上帝类的长法。兑现物是删掉 10 处
`SegmentFinder(get_default_base_dir())` 构造样板——层只出模块函数，不出句柄。

---

## 4. 读侧（R）

| 编号 | 规则 | 备注 |
|---|---|---|
| R1 | **出结构，不出字节、不出句柄** | 返回 dataclass / NamedTuple / ndarray / 基本类型；不出 `IO`、不出裸 `bytes` |
| R2 | **不出目录、不出存储根**；载荷字节的取用位置照出 `Path` | 出目录 = 把布局复制给调用方，且门禁抓不到（`dir / "x"` 是普通拼接） |
| R3 | **只解析本域格式，不做业务判断，不出领域异常** | 阈值、该不该删、算不算停顿、映射成什么状态码，一律留调用方。多态失败用**事实枚举 + NamedTuple**（`Vod(state, text, segments)`），不是 `Optional[str]` |
| R4 | **落盘状态是事实，可做过滤参数**；选错会静默出错的参数**必填、无默认** | `ids(order=)` 可给默认（选错只影响清单顺序）；`read_segment` 的 `width`/`height` 不给默认（下游会变成 train-serve skew） |
| R5 | **一次调用一次扫盘** | 按「一次调用 = 一件事」切函数；不照搬取值器，也不合成跨口径黑盒 |
| R6 | **环境坏了原样抛，内容坏了逐行隔离** | 打不开 / 写不进 / 建不了目录 → `OSError` 原样抛；单条记录解析不出 → 跳过 + warning，不让一行毁掉整个文件 |

**R4 与 R5 打架时，切函数优先**：hls 读侧曾设计成 `list_segments(..., playable_only=)` 必填
bool，改成了 `list_segments`（盘上有哪些段）+ `playable_segments`（已登记、带 EXTINF）两个
函数——过滤判据与段时长同源于清单键集合，bool 参数方案下调用方拿到段之后还要再读一次
playlist。代价如实记账：类型上挡不住「直接用 `list_segments` 忘了滤」，防护退化成
docstring + review。

**字节的转换归谁做**：

| 情形 | 归谁 |
|---|---|
| 只转发字节（`/media/*` 送 mp4 给浏览器） | 层出 `Path` |
| 有结构的编解码（`.idx` / playlist / `features.jsonl` / fMP4 fragment） | **层内转**，含为此起外部编码器（D3） |
| 解码（段 mp4 → 像素帧） | **层内转**，`read_segment` / `iter_frames` 与 `insert_segment` 互为逆运算 |

---

## 5. 写侧（W）：三条路线

**调用方一律只交内存对象、只调一次函数**——路线是层内实现，不出现在签名上。对外形态就是
`INSERT`：给一行数据，剩下的是存储的事。

```text
路线 A  transaction（外部工具产出 / 有位置依赖）
    ① stage    锁外，层内起编码器在 {step}/{domain}/.stage_{产物键}/ 造产物
    ② adjust   读既有状态、做位置相关的最后修补（无位置依赖则 ①→③）
    ③ commit   原子 rename + 登记
  任一步异常 → 删 stage、不 rename、不登记，原异常上抛

路线 B  append（追加）
    收内存对象 → 序列化 → 一次 open(mode="a") + write

路线 C  overwrite（层内纯序列化，整体替换）
    ① 编码     整批先编码完（失败时盘上一个字节没动）
    ② 写 tmp   与目标同目录的 .{name}.tmp
    ③ rename   os.replace 原子换名
```

**A 与 C 的分界**：产物字节由**外部工具**造、或有位置依赖 → A；层内从内存对象**纯序列化**
→ C。

| 编号 | 规则 | 路线 |
|---|---|---|
| W1 | **stage / tmp 必须与目标同卷**（落在 `{step}/{domain}/` 之下），不得用 `tempfile.mkdtemp()` | A C |
| W2 | **目标文件名在 rename 前不得存在**——名字一出现就是合法产物 | A C |
| W3 | **位置相关的修补必须在 rename 之前** | A |
| W4 | **失败即整体作废**：删 stage/tmp、不 rename、不登记，原异常上抛 | A C |
| W5 | **一次调用写完调用方给的一批，层内不攒批** | B |
| W7 | **stage 目录名取「与产物同键」，不用随机 nonce** | A |
| W8 | **登记顺序：索引先于主产物可见，主产物先于清单条目** | A |

> W6 不存在（原条文已删，编号不复用——别的文档还在引 W1–W5）。

**W7 的取舍**（`.stage_{track}_{ts_us}` 而不是 `.stage_{nonce}`）：同键 = 同产物名，撞键在
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
| `features.jsonl` | **B** | 追加型，没有"先造好一个完整产物"这回事 |
| `facts.jsonl` | **C** | 整批替换，内容由层内序列化 |
| `metadata.json` | **C** | 读改写，没有追加形态 |
| `raw_segment_*.idx` | **B** | **具名例外**：可走 A，但它不匹配段正则、对读侧不可见、无中间态问题，走 A 只是多两步。必须写进域 docstring，否则后人当漏改 |

---

## 6. 并发：本层不持锁

**顺序是构造出来的，不是抢出来的。**

```text
同代次内的顺序   由 app/utils/task_queue.py 的 SerialTaskQueue 提交序构造
跨代次的隔离     由调用侧的对象引用判等构造（乐观锁，失败即丢弃）
```

同一 step 的写与 `tasks.purge_step` 提交到同一条队列，跨域删除因此不用单开机制。详见
[DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md)。

**为什么不是层内加锁**：锁保证的是「不重叠」，而 supersede 需要的是「旧 run 已终止」——一个
还在写的 run，锁只能让 `rmtree` 等一下，等完了它继续写，僵尸 step 照样出现。**互斥挡不住一
个没停的写者。**

代价如实记账，两条：

- 这条保证**不在层内、门禁抓不到**。各域写入口的 docstring 必须写明它依赖这个前提。
- **队列不能加 worker**。加了不报错，表现是 tfdt 碰撞、旧段串进新 run。配置里不给这个旋钮 +
  `SerialTaskQueue` 类名 + 三处注释共同守着。

---

## 7. 依赖与状态（D）

| 编号 | 规则 |
|---|---|
| D1 | **模块级默认 stdlib-only**，重依赖走 D2。唯一例外是**域货币**（该域每个签名都在收发的类型，如 `feature` 的 `FrameFeature`，随之带进 numpy），它没法推迟到函数体内，必须在 `tests/test_import_hygiene.py` 的 `BUDGET` 里单独登记上限。`hls` 域 14 个成员里只有 1 个出 `ndarray`，那不是货币、走 D2 |
| D2 | 单个重函数要的依赖走**函数体内 import**，别让整个域为它变重 |
| D3 | **编解码可起外部工具子进程**，三个条件：① 必须有超时；② 失败即整体作废（W4），不留半成品；③ 只用于编解码——调度、编排、并行度一律不进层 |
| D4 | **可持有**解析缓存；**不可持有**批缓冲、待写内容、owner fence、连接、任何要 `start()/stop()` 的东西。**不出句柄** |
| D5 | **外部工具的可用性是运行时依赖，不是 import 期依赖**：`import app.storage.hls` 不得要求机器上有 ffmpeg；缺二进制在调用 `insert_segment` 时才炸，读侧不受影响 |

---

## 8. 测试（T）

| 编号 | 规则 |
|---|---|
| T1 | **每个 codec 必须有往返测试**——这是转换契约唯一能被验证的地方。写进去的内存对象读回来要相等；层是双向的，往返链条一直闭合到「段」这一档 |
| T2 | `ts_to_us` 的往返断言在 **us 域**闭合：`parse(name(ts)) == int(ts * 1e6)`，**不是** `== ts`。截断有损，读侧段级定位的 `bisect_right - 1` 正建立在它上面 |
| T3 | **事务不变式必须能脱离外部工具测**：保留私有的 commit 入口，喂手工造的最小合法产物，断言 W2 / W4 / W8。否则最该测的断言躲在需要 ffmpeg 的函数背后，CI 缺二进制时静默失效 |
| T4 | **需要外部二进制的测试单独分档**（marker）。层的主体测试必须毫秒级、无外部依赖——它是所有人的下游，跑得慢就没人跑 |
| T5 | **并发有一条真测试**：多线程同时写同一冲突键，断言产物齐备、清单行数正确、位置相关字段（如 tfdt）不碰撞。测试碰私有面在 T3/T5 是正当的——被测的是内部不变式，不是公开契约 |

现有覆盖见 `tests/test_storage_tasks.py` / `test_storage_feature.py` / `test_storage_hls.py`
（三者合计 244 条）与 [TESTING_MAP.md](TESTING_MAP.md)。

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
| 依赖白名单（只许 `app.storage` / `app.domain` / `app.settings`） | ✅ 已有（白名单式，反向探针验过） |
| D1 模块级 stdlib-only | ✅ 逐模块 `BUDGET` + 覆盖检查，新域文件漏登记就红 |
| T1 / T2 往返测试 | ✅ 每个 codec 一条，写不出来说明正反运算没同居 |
| T3 事务不变式 / T5 并发 | ✅ T5 是概率性的，但没有串行保证时必红 |
| L2 外部输入经 `parse_*` | ✅ `parse_segment_name` / `parse_init_name` 对 `../`、绝对路径、非法 track、非数字 ts 返回 `None`；`segment_path` 入参不收裸 `str` |
| R4 正确性参数必填 | ✅ 漏传 `TypeError` |
| D3 子进程必须带超时 | ✅ AST 扫 `subprocess.*` 调用有无 `timeout=` |
| L5 `settings.storage_base_dir` 访问面收敛 | ⏳ 迁移阶段 6（现在加会立刻红） |
| 文件名字面量不出层 | ⏳ 迁移阶段 6。**AST 扫 `ast.Constant` 并跳过 docstring**，不能 grep（`_segment_` 会命中 30 多处 `ca_segment_len`） |
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
- **`purge_step` 与各域写者靠读写锁互斥**：该方案已废，按 §6 读。

## 代码来源

- `app/storage/__init__.py`（边界清单与设计约束，与本文 §0 / §9.1 必须一致）
- `app/storage/_root.py`（`DOMAINS` 白名单、`path()` 逐级下钻、根解析记忆化）
- `app/storage/tasks.py`（`steps` / `ids` / `purge_step`）
- `app/storage/feature.py`（`features.jsonl` 读写，路线 B）
- `app/storage/hls/`（`types` / `_layout` / `_encode` / `_decode` / `_fmp4` / `_m3u8` /
  `_idx` / `_meta` / `_write` / `_read`）
- `app/utils/task_queue.py`（`SerialTaskQueue`，§6 的串行保证）
- `tests/test_import_hygiene.py`（依赖白名单 + 导入预算门禁）
- `tests/test_storage_{tasks,feature,hls}.py`
- 变更记录：`docs/update/20260909_STORAGE_LAYER_BASE.md`（第 1 期）、
  `20260909_STORAGE_FEATURE_DOMAIN.md`、`20260911_STORAGE_HLS_DOMAIN.md`、
  `20260912_HLS_READ_CAPABILITY.md`、`20260911_STEP_INIT_SUPERSEDE.md`
