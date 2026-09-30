> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据层 `app/storage/` 设计规范

本文是**准入判据**，不是现状描述：新增域文件、新增成员、review 存储层 PR 时按它判。盘上布局、各产物的写者与读者见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。

> **编号被代码引用**：章节号 §0–§9、条文 R/W/L/D/T、路线 A/B/C、设计约束 1–8 出现在 `app/storage/` docstring 与 `tests/test_storage_*.py` 里。改条文可以，改编号先搜引用。

---

## 0. 层的契约：内存对象进，内存对象或 `Path` 出

```text
调用方（services / routers）
   │  交内存对象 + RunIdentity                      拿内存对象 / 已定位的 Path
   ▼                                                        ▲
┌────────────────────────────────── app/storage/ ──────────────────────────────────┐
│  hls/            inference/                 lab(未建)   tasks.py     runs.py      │  §1 分域
│   types.py        (货币在 app.types)                     (跨域枚举)   (分配/查询)  │  §2 域容器
│   _write / _read / _encode / _decode / _m3u8 / _fmp4 / _idx / _timeline          │  §4 §5 转换
│   hls._layout.domain_dir ───────┐                                                │
│   inference._layout.domain_dir ─┴─▶ utils.root.domain_dir(run, domain, create)   │  §3 定位
│   utils.fs   整体替换 / 原子删除 / 建一级目录（全层唯一一份盘上原语）           │
└──────────────────────────────────────────────────────────────────────────────────┘
   ▼
{root}/{task}/{step}/{run_id}/{hls|inference}/
```

- **契约**：文件名、目录布局、文本与二进制格式、编解码全在层内，上层一样不碰。层是双向的：写进去 `Sequence[Frame]`，读出来是 `Frame`。
- **位置**：与 `app/services/` 平级、在它下面一层。它是数据层不是服务，同级先例是 `app/types/`、`app/db/`。
- **依赖白名单**：stdlib、三方、`app.types`、`app.settings`（落盘根的唯一来源，只在函数体内 import）。其余 `app.*` 一律不行，包括 `app.db`——它进来不造环、不报错，只会把数据层绑死在 ORM 上。门禁 `tests/test_import_hygiene.py::test_layer_package_imports_only_whitelisted_app_modules`。写侧、读侧与 cleanup daemon 能同时向下依赖本层，前提就是它不向上、不向旁伸手。

### 准入四问是本层唯一的边界判据

```text
它是「内存模型 ↔ 盘上字节」的转换本身吗？            进层
它是关于这个转换的策略（做不做、重试几次、留多久）？  不进
它是编排（谁调、何时调、排队、并行度）？              不进
它是业务语义（HTTP 状态码、告警阈值、算不算停顿）？    不进
```

「失败要不要重试」是策略，不进层；「这次失败是环境坏了还是数据坏了」是层内事实，层必须能答（R6），否则上层没有重试依据。

不进本层的东西（`app/storage/__init__.py` docstring 的边界段是它的摘要，改一处同步另一处）：

| 不进的东西 | 归谁 | 判据 |
|---|---|---|
| 检测结果批缓冲 | `ClientQueues.ca_detections` + recording 的 detections 队列 | 有状态 / 编排（D4） |
| 事实合并规则（保留谁） | `OfflineRunner._replace_segments` | producer 语义，不是格式事实 |
| run 的分配时机与互斥 | `app/services/run_control/`（`lock_for` 内调 `allocate`） | 编排。生成 run_id + 建 run 目录本身**进层**（`runs.allocate`）：目录名即身份 |
| TTL 天数 / 扫描周期 / 删不删 | `app/daemons/cleanup/` | 策略（删除动作经层内原语 `utils.fs.remove`） |
| 失败要不要重试 | 调用方 | 策略 |
| MediaToken / HTTP 状态码 / token 化 URL | `app/routers/` | 业务语义 |
| 异步调度（队列 / WorkerPool / strategy 分发） | 编排方 | 编排 |
| VOD 清单文本 | `app/services/utils/vod_playlist.py` | 装配产物，不是资源元数据（§2） |
| Label Studio 运行时配置 | `app/services/lab/runtime_config.py` | 是配置不是产物，没有 run 身份键 |

常被误认为不属于这里、实际**在层内**的：ffmpeg 转码参数 / tfdt hex-patch / timescale pin、`cv2.VideoWriter` 与 `eff_fps` 反推、段解码、为编解码起外部工具子进程。放在层外会静默出错：`eff_fps` 一个值同时决定媒体时长、EXTINF、tfdt（见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)），在层外算就要分三条路跨边界传，写岔一个都不报错。

---

## 1. 分域：一域一个 import 名，产物按同一组域名分目录

对外按域分，不按读写分。`read.py` / `write.py` 会把「盘上有哪些 step」与「这个 step 有哪些段」两个抽象层级挤进同一模块；按域分则每个函数自带域限定词，`hls.*` 不可能被要求回答 `detections.jsonl` 在哪。现有域：`hls/`、`inference/`（子包）、`tasks.py` / `runs.py`（跨域单文件）、`lab.py`（未落地）、`utils/`（包内私有底座）。

- **子包按产出层切模块，不按产物拆平级域**：`inference/` 的两份产物有两对独立 codec、两条写路线（B / C），故 `_detection` / `_temporal` 分文件；但它们共用时间轴与行框定，拆成平级域反而要跨域配对。
- **薄域单文件、重域子包，对外看不出区别**：调用方分不出 `hls` 是包还是模块，薄域变重是非破坏性升级。代价：facade `__init__` 的 re-export 会连带加载实现模块，故实现模块的**模块级只许 stdlib + `app.types`**（D1）。
- **域读写口以 `run: RunIdentity` 开头且只收它**，不收 `(task_id, step_id)` 散标量。传错在 `utils.root.domain_dir` 即 `TypeError`，且不建目录。`RunIdentity` 只由 `runs.allocate` / `runs.query` 产出，调用方拼不出不存在的身份。
- **跨域模块不出定位能力**：`tasks` / `runs` 只在跨所有域时出面，以 `task_id` 或 `(task_id, step_id)` 开头。本层不提供脱离身份键的产物读写。

### 成员命名：动词前缀是封闭集合

选不出前缀，说明这个成员的职责还没想清。

```text
list_<复数>       枚举盘上事实 → 列表，一次 iterdir / 一次读
read_<单数>       读一份产物 → 内存对象
iter_<复数>       流式产出，O(1) 内存
query_<问题>      从盘上事实推出一个答案 → 句柄 / 布尔 / 汇总形状（可跨多次读盘）
insert_<单数>     事务式写一份（路线 A）
append_<复数>     追加一批（路线 B）
write_<单数>      整体替换（路线 C）
allocate          分配一个新身份并建出它的目录（全层唯一，runs.allocate）
<名词>_path       定位
<名词>_name / parse_<名词>_name    名字的编解码对（互为逆运算）
```

集合外的既有名字：`runs.successor`、`tasks.latest_run_id`。

- **对外不出删除成员**：换代靠一 run 一目录（§6），回收只有 TTL。`utils.fs.remove` / `purge_trash` 是底座原语，不受本集合约束，唯一包外调用方是 `app.daemons.cleanup`。
- **过滤条件进名字，不进 bool 参数**：`list_segments_in_range`，不是 `list_segments(start_ts=, end_ts=)`（见 §4 R4/R5）。
- **返回 id 列表就在名字里说明**：`list_step_ids` 返回 `List[int]`，不叫 `steps`。
- **同义动词只留一个**：读产物一律 `read_`（不用 `load_` / `get_`），读侧派生答案一律 `query_`。域内成员可省宾语（`hls.insert_segment`），跨域模块不能省（`tasks.list_step_ids`）。

### 路径隔离靠四道执行机制，不是约定

要求：step 下只有 run 目录；run 下只有 `DOMAINS` 域目录、没有文件；存储根下只有数字 task 目录与回收区 `.trash/`。层外自己拼路径的 `.lab_exports/`、`lab_runtime_config.json` 不符合它。

| 机制 | 挡什么 | 不挡会怎样 |
|---|---|---|
| `domain` **必填、无默认** | 漏传 | 文件写回 run 根、退回平铺，说不清哪个文件归谁。现为 `TypeError` |
| `utils.root.DOMAINS` **白名单** | 打错 | `"feature"` / `"HLS"` 静默造出多余子目录：写侧不报错、读侧只是查不到、TTL 照删，连残留证据都不留。现为 `ValueError` |
| **写者不建父目录** | 回收后的迟到写 | run 目录被回收后，迟到的写把它重建成只含半截产物的僵尸目录。现为 `FileNotFoundError`（§3） |
| **路径不跨代复用** | ABA | `run_id` 同 step 内严格递增（`max(now_ms, 已有最大 + 1)`，在调用方串行点内分配）。否则「目录在」不再等于「我那一代还在」，上一条就放行了错的写 |

- 校验早于 `mkdir`（`tests/test_storage_tasks.py::test_unknown_domain_creates_nothing`）。新增一个域要改 `DOMAINS` 常量，这是有意的显式决策。
- **域名是布局，产物文件名是内容**：白名单归 `utils/root.py`，文件名归各域。
- **目录名能当时间用，就别读 mtime**：写产物只刷新最深一级目录的 mtime。「最近活动」取最大 `run_id`（`tasks.latest_run_id`，不下钻）；TTL 判据取 `{step}/` 的 mtime（其直接子项只有 run 目录，故它 = 最近一次分配）。两个口径不能共用一个函数。

---

## 2. 域容器：`types.py` 集中声明本域收发的形状

| 问 | 归属 | 例子 |
|---|---|---|
| 换掉落盘格式，它会不会跟着消失？ | 会 → **域 `types.py`** | `SegmentRef` 由文件名解出；`Segment` 的身份与时长来自清单同一行 |
| 不会，且是全仓通用的内存契约 | `app/types/` | `Frame` / `FrameDetection` / `TemporalSegment` / `LabelProbs` / `RunIdentity` |
| 是给别人消费的**装配产物**，不是资源元数据 | 出域 | `VodEntry` → `app/services/utils/vod_playlist.py` |

判据不在「跨不跨服务」：让 `app.types` 认识 `SegmentRef`，等于让最上游的契约层持有最下游的文件名格式知识。

1. **`types.py` 是子包的底**：stdlib only，不 import 同包任何模块。故 `TRACKS` / `require_track` 留在 `_layout`——那是布局词汇表不是形状，搬进来会让 `types` 反向依赖 `_layout`。
2. **只放对外契约**：函数内部传参的小 NamedTuple 放在用它的地方旁边。
3. **消费方 < 2 不进**（设计约束 2）。反例：`Step` 摘要只有 `app/routers/task.py` 一个消费方。正例：`HlsSpan` 有 task / traceback / lab 三个消费方，段跨度算法因此只剩一份。没有「从文件名解出的身份」这类形状的域不建 `types.py`（`inference/` 的货币全在 `app.types`）。
4. **同名不同义要避开**：`step` 已是业务实体（`clean_task.current_step`、`clean_alarm.step_id`），域内不定义同名类型。

---

## 3. 定位（L）：路径集中在层内，业务层不拼路径

```text
查询 / 外部输入 ──parse_*──▶ 结构 ──定位──▶ Path ──▶ 外部工具读写
                              ↑ 校验只在这一步
```

| 编号 | 规则 | 不这么做会怎样 |
|---|---|---|
| L1 | **定位函数收结构，不收散标量**：`segment_path(run, ref)` | 读写两侧各拼一次文件名，迟早分叉 |
| L2 | **外部输入先经 `parse_*` 转结构，路径由结构重建** | path traversal 从「靠校验挡」变成结构上不可能。`endswith("init.mp4")` 会放行 `evil_init.mp4`（`app/routers/media.py` 走 `parse_init_name`） |
| L3 | `SegmentRef` **带 `track`、不带 `task_id` / `step_id` / `run_id`** | track 在文件名里、必须能解出；身份键由调用方以 `RunIdentity` 持有 |
| L4 | **域根一律经 `utils.root.domain_dir(run, domain)`**，域名在一个域文件里只出现一次 | 域名散落之后，白名单救不了拼错的那一处 |
| L5 | **域文件不得自己读 settings、不得自己拼根** | 根解析只有 `utils.root._storage_root()` 一处 |

- **定位与建目录分开**：`utils/root.py` 的 `path` / `run_path` / `domain_dir` 只定位；唯一建目录的是 `domain_dir(create=True)`，经 `fs.ensure_dir` 只建域这一级（不带 parents）。run 目录只由 `runs.allocate` 建——写者建父目录 = 回收后的迟到写重建出僵尸目录。
- **`path(task_id, step_id)` 逐级下钻**，跳级（给了 step 省了 task）抛 `ValueError`，否则静默少一层目录。
- **删除只在 `utils/fs.py`**，枚举只在 `root.run_ids` 与 `tasks`。`utils/` 包内私有：交出 `root` 就交出了「根 + 两级 id + run + 域」的拼装能力。
- **域文件样板**照抄 `utils/root.py` docstring（`_DOMAIN = "hls"` + 一个转调 `_root.domain_dir` 的 `domain_dir`）。helper 别命名成 `_root`：会遮掉模块别名，立刻 `AttributeError`。
- **根解析 `_storage_root()` 必须记忆化，且 key 取 `settings.storage_dir` 原始字符串**：`storage_base_dir` 每次访问都跑 `.resolve()`；`tmp_storage` fixture 靠 patch `settings.storage_dir` 把读写两侧指到临时目录。写成模块级常量会让 patch 失效，表现是测试去写真实 `database/`。
- **路径出层、根不出层**（设计约束 3）：不出路径，就得把 `FileResponse`、送标导出这类动作都搬进层，那是上帝类的长法。层只出模块函数，不出「带根的查找器」句柄。

---

## 4. 读侧（R）

| 编号 | 规则 | 备注 |
|---|---|---|
| R1 | **出结构，不出字节、不出句柄** | 返回 dataclass / NamedTuple / ndarray / 基本类型；不出 `IO`、裸 `bytes` |
| R2 | **不出目录、不出存储根**；载荷字节的取用位置照出 `Path` | 出目录 = 把布局复制给调用方，且门禁抓不到（`dir / "x"` 是普通拼接） |
| R3 | **只解析本域格式，不做业务判断，不出领域异常** | 阈值、该不该删、映射成什么状态码都留调用方。多态失败用事实枚举 + NamedTuple（`Vod(state, text, segments)`），不用 `Optional[str]` |
| R4 | **落盘状态是事实，可做过滤参数**；选错会静默出错的参数**必填、无默认** | `list_task_ids(order=)` 可给默认（只影响顺序）；`read_segment` 的 `width` / `height` 不给默认（下游会变成 train-serve skew） |
| R5 | **一次调用一次扫盘** | 按「一次调用 = 一件事」切函数；不照搬取值器，也不合成跨口径黑盒 |
| R6 | **环境坏了原样抛，内容坏了逐行隔离** | 打不开 / 写不进 / 建不了目录 → `OSError` 原样抛；单条记录解析不出 → 跳过 + warning |

- **R4 与 R5 冲突时切函数**：过滤判据与返回字段同源时（段是否已登记、段时长都来自清单），bool 参数方案会让调用方拿到段后再读一次清单。
- **一个域里「有哪些 X」只许一个入口**：hls 段枚举只有 `list_segments`（读清单）及其区间切片 `list_segments_in_range`，同源、同返回类型；文件系统枚举不留，降级成私有也不行（`__all__` 拦不住包内误用）。能删掉一个口径，就别留着靠约定。
- **字节转换归层内**：只转发字节（`/media/*` 送 mp4）时层出 `Path`；有结构的编解码（`.idx` / playlist / jsonl / npz / fMP4）与解码（段 → 像素帧，`read_segment` / `iter_frames` 是 `insert_segment` 的逆运算）都在层内，含为此起外部工具（D3）。

---

## 5. 写侧（W）：三条路线

调用方只交内存对象、只调一次函数；路线是层内实现，不出现在签名上。

```text
路线 A  transaction（外部工具产出 / 有位置依赖）
    ① stage    层内起编码器，在 {run}/{domain}/.stage_{产物键}/ 造产物
    ② adjust   读既有状态、做位置相关的最后修补（无位置依赖则 ①→③）
    ③ commit   原子 rename + 登记
  任一步异常 → 删 stage、不 rename、不登记，原异常上抛

路线 B  append：收内存对象 → 整批序列化 → 一次 open(mode="a") + write

路线 C  overwrite：整批先编码 → 写同目录 .{name}.tmp → os.replace
  全层唯一实现 utils.fs.replace(path, write_fn)：不建父目录；任何异常先删 tmp 再原样上抛
```

**A 与 C 的分界**：产物字节由外部工具造、或有位置依赖 → A；层内从内存对象纯序列化 → C。

| 编号 | 规则 | 路线 |
|---|---|---|
| W1 | **stage / tmp 必须与目标同卷**（落在 `{run}/{domain}/` 之下），不得用 `tempfile.mkdtemp()` | A C |
| W2 | **目标文件名在 rename 前不得存在**——名字一出现就是合法产物 | A C |
| W3 | **位置相关的修补必须在 rename 之前** | A |
| W4 | **失败即整体作废**：删 stage / tmp、不 rename、不登记，原异常上抛 | A C |
| W5 | **一次调用写完调用方给的一批，层内不攒批** | B |
| W7 | **stage 目录名取「与产物同键」，不用随机 nonce** | A |
| W8 | **登记顺序：索引先于主产物可见，主产物先于清单条目** | A |

> W6 空号，不复用。

- **W4 的唯一例外是 raw sidecar**：`.idx` 写失败只 warning、段照常提交（它只服务离线反查，读侧容忍缺失）。tfdt 修补失败则必须整段作废，没修好的 fragment 进清单会覆盖前段画面（`hls/_write.py`）。
- **W7**：同键 = 同产物名，撞键在 `os.replace` 那步本就是既有 bug；随机 nonce 每次崩溃都留一个无人回收的目录，同键则重试自然复用（入口 `rmtree` 一次即幂等），目录名还自带「哪份产物没写完」。
- **W8**（写反都不报错）：索引后于主产物会留下「段可见但索引未就位」的窗口；清单条目先于主产物 = 清单有行无文件，播放器报错；清单头（含 `EXT-X-MAP`）先于任何条目行。

| 产物 | 路线 | 理由 |
|---|---|---|
| `{track}_segment_*.mp4` / `{track}_init.mp4` | **A** | 外部编码器产出；有位置依赖（tfdt = 前面所有 EXTINF 之和） |
| `{track}_playlist.m3u8` 的 `#EXTINF` 行 | **B** | 一行文本，作为 A 的 ③ 登记步执行 |
| `detections.jsonl` | **B** | 追加型 |
| `temporal.jsonl` | **C** | 多种事实共居、整批替换；保留谁归调用方 read → merge → write |
| `label_probs.npz` | **C** | 二进制整体替换，`np.savez` 写文件对象（避免自动补后缀） |
| `raw_segment_*.idx` | **C** | float64 数组纯序列化，作为 A 的 ③ commit 第一步执行（W8） |

---

## 6. 并发：本层不持锁

```text
同一 run 内的顺序   由调用侧的串行点构造（SerialTaskQueue 提交序 / 单个离线进程）
跨 run 的隔离       由路径不跨代复用构造：一 run 一目录，写者只写自己 RunIdentity 指向的目录
run 的分配          由调用方的串行点构造（allocate 在 lock_for 内，run_id 才严格递增）
回收                只有 TTL 整 step，经 utils.fs.remove 原子 rename；写者不建目录，迟到写原子失败
```

- **回收与读写无需互斥**：换代不删任何东西，旧一代迟到的写落进它自己的 run 目录；回收时 run 目录整体 rename 进回收区，迟到写 `FileNotFoundError`，锁定该 run 的读者拿到「不存在」。原则见 [DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md) §3.1。
- **不在层内加锁**：锁保证「不重叠」，换代需要「旧写者已终止」。对还在写的写者，锁只能让删除等一下，等完它继续写，僵尸目录照样出现；能挡住它的是不删它的目录、不让它建目录。
- **代价一：保证不在层内、门禁抓不到**。各域写入口的 docstring 必须写明依赖这个前提。
- **代价二：写队列不能加 worker**。加了不报错，表现是同一 run 内 tfdt 碰撞（后段覆盖前段）。由 `config/recording_config.yaml` 不给 `workers` 旋钮 + `SerialTaskQueue` 类名 + 注释共同守着。

---

## 7. 依赖与状态（D）

| 编号 | 规则 |
|---|---|
| D1 | **模块级默认 stdlib-only**。唯一例外是**域货币**（该域每个签名都在收发的类型，如 `hls` 的 `Frame`、`hls._idx` 的 float64 数组、`inference._temporal` 的 `LabelProbs`，随之带进 numpy），必须在 `tests/test_import_hygiene.py` 的 `BUDGET` 里逐模块登记。非货币重依赖（cv2）一律走 D2 |
| D2 | 单个重函数要的依赖走**函数体内 import**，别让整个域为它变重 |
| D3 | **编解码可起外部工具子进程**，三个条件：① 必须有超时；② 失败即整体作废（W4）；③ 只用于编解码，调度 / 编排 / 并行度不进层 |
| D4 | **可持有**解析缓存；**不可持有**批缓冲、待写内容、owner fence、连接、任何要 `start()/stop()` 的东西。**不出句柄** |
| D5 | **外部工具可用性是运行时依赖**：`import app.storage.hls` 不得要求有 ffmpeg；缺二进制在调 `insert_segment` 时才炸，读侧不受影响 |

---

## 8. 测试（T）

| 编号 | 规则 |
|---|---|
| T1 | **每个 codec 必须有往返测试**：写进去的内存对象读回来要相等，链条一直闭合到「段」这一档 |
| T2 | `ts_to_ms` 往返在 **ms 域**闭合：`parse(name(ts)).ts_ms == floor(Fraction(ts) * 1000)`，且 `Fraction(ms, 1000) <= Fraction(ts) < Fraction(ms + 1, 1000)`。不是 `== ts`，也不能用 `int(ts * 1000)` 作期望值（`0.29 * 1000 == 290.0`，而 0.29 精确值略小）。读侧段级定位的 `bisect_right - 1` 建立在「段名 ≤ 首帧」上 |
| T3 | **事务不变式必须能脱离外部工具测**：把外部工具那一步打桩（`tests/test_storage_hls.py::fake_pipeline` 替换 `_encode.write_mp4v` / `_fmp4.transcode`），断言 W2 / W4 / W8。否则 CI 缺二进制时最该测的断言静默失效 |
| T4 | **需要外部二进制的测试单独分档**，缺料即跳过（现为 `skipif(not _external_tools_available())`）。主体测试必须毫秒级、无外部依赖 |
| T5 | **并发要有一条真测试**：多线程同时写同一冲突键，断言产物齐备、清单行数正确、tfdt 不碰撞。T3 / T5 碰私有面是正当的——被测的是内部不变式 |

---

## 9. 设计约束与门禁

### 9.1 新增成员前过一遍（编号被代码引用）

1. **一域一个 import 名**，函数带域限定词。域内怎么拆文件是域自己的事。
2. **准入判据**：这个知识有几个包会因为它变了而出错？< 2 → 不进。
3. **路径出层，根不出层。**
4. **不出句柄，只出模块函数。** 层可以持有层内状态，但不交出去。
5. **能用参数解决的不拆方法**；但**涉及正确性的参数不给默认值**——漏传是 `TypeError`，不是静默走错分支。
6. **朴素**：只回答格式事实，不做业务判断、不定义错误语义。
7. **能力太窄的不包装成接口**：调用方自己一行就能写对的动作，包装它只是多一层。
8. **并发由调用侧的串行调度保证，本层不持锁**（§6）。

### 9.2 门禁映射

| 规则 | 可执行性 |
|---|---|
| L4 域名白名单、`domain` 必填；R4 正确性参数必填 | ✅ 漏传 `TypeError` / 打错 `ValueError` |
| 依赖白名单 | ✅ `LAYER_PACKAGES`（白名单式） |
| D1 模块级 stdlib-only | ✅ 逐模块 `BUDGET` + 覆盖检查，新域文件漏登记就红 |
| T1 / T2 往返、T3 事务不变式 | ✅ |
| L2 外部输入经 `parse_*` | ✅ `parse_segment_name` / `parse_init_name` 对 `../`、绝对路径、非法 track、非数字 ts 返回 `None` |
| T5 并发真测试 | ❌ 未实现（`tests/test_storage_hls.py` 模块 docstring 注明本期无断言） |
| D3 子进程带超时 | ⚠️ 只靠 review，无自动门禁。现有两处都带：`_fmp4.transcode` 传 `timeout=`，`_decode` 用 `threading.Timer` 看门狗 |
| D3 另两条、R1 / R3 | ⚠️ 类型标注 + review |
| L5 `settings.storage_base_dir` 访问面收敛 | ⏳ 未加。层外仍直读：lab 临时件根与运行时配置、cleanup 扫描根 |
| 文件名字面量不出层 | ⏳ 未加。只能 AST 扫 `ast.Constant` 并跳过 docstring，不能 grep（`_segment_` 会大量命中 `ca_segment_len`） |
| **W 的路线选择（A / B / C）** | ❌ **不可测，只能 review**。走错不会红，表现是「中间态被读到」。新增域文件的 PR 必须写明每类产物选了哪条路线及理由 |

---

## 10. 旧编号与反例

`tests/test_storage_hls.py` 里的旧编号：`规范 §7.6` = 本文 §8；`规范 §7.8`（解码进不进层）= 已决进层，见 §4。

不要再照着实现：

- **层内 per-`(task, step)` 锁**：换代要的是旧写者已终止，不是不重叠（§6）。
- **「storage 只管名字与定位，内容读写不进层」**：编解码是本层本职（§0）。
- **删除与写者靠读写锁互斥**：回收与写者不互斥，靠写者不建目录 + 原子删除（§6）。

## 代码来源

- `app/storage/__init__.py`（边界清单与设计约束，与 §0 / §9.1 同步）、`app/storage/utils/{root,fs}.py`、`app/storage/{tasks,runs}.py`
- `app/storage/hls/`（`types` / `_layout` / `_encode` / `_decode` / `_fmp4` / `_m3u8` / `_idx` / `_write` / `_read` / `_timeline`）、`app/storage/inference/`
- `app/services/utils/task_queue.py`（`SerialTaskQueue`）、`config/recording_config.yaml`
- `tests/test_import_hygiene.py`（`LAYER_PACKAGES` / `BUDGET`）、`tests/test_storage_{tasks,runs,fs,hls,inference,cleanup_ttl}.py`（覆盖清单见 [TESTING_MAP.md](TESTING_MAP.md)）
- 设计推导：`docs/update/20260909_STORAGE_LAYER_BASE.md`、`20260927_STORAGE_RUN_DIR_PROPOSAL.md`
