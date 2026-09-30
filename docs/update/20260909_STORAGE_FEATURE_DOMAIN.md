# `storage/feature.py`：features 域落地（分期改造第 3 期，零调用点改动）

> **变更状态**：生效中（2026-09-09）　<!-- 新模块已落地并有单测/门禁覆盖；本期同样不接调用点，模块暂无生产消费方 -->
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)（2026-09-20）
>
> <!-- 承接 20260909_STORAGE_LAYER_BASE.md（第 1 期）。期序有调整，见「变更背景 / 为什么先做 feature」。 -->

## 概述

新建 `app/services/storage/feature.py`，把 `{step}/features/` 下两份 JSONL 产物
（`features.jsonl` / `facts.jsonl`）的**定位、编解码、读写**收进 storage leaf。
`inference/feature/store.py` **一行未动**——本期仍是零调用点改动，迁移在第 4 期。

同时补上第 1 期记录里点名的门禁洞：导入预算改为**逐模块登记**，并加一条覆盖检查，
新增域文件不登记就红。

一句话的边界：**本模块管「文件在哪、一行是什么、坏行怎么办、整体替换怎么原子」，
不管「攒多少行才写、这次写属于哪个 run、失败要不要打断主链路」**。

## 变更背景

### 为什么先做 feature，不做 hls

第 1 期定的顺序是「2 期 hls → 3 期 feature/lab」。本期按开发者指示先做 feature；
`hls.py` 已有设计提案（[20260909_STORAGE_HLS_DOMAIN](20260909_STORAGE_HLS_DOMAIN.md)）但尚未动代码。
两者无依赖关系（各自绑死自己的域名、各自读 `_root.path`），换序无技术代价；且
feature 域更小、货币更清晰，作为**第二个域文件**来验证第 1 期 §7 那份草稿规范，
反馈更快。§7 那句「第 2 期落 `hls.py` 时回填修正」相应改为由本期回填（见下方 §5）。

### 现状：features 域的知识全部只有一个主人，但那个主人同时还持有状态

`inference/feature/store.py` 一个文件里叠了四层：路径拼装（`_path`）、JSONL 行框定
（`_write` / `load` 的逐行解析）、`FrameFeature ↔ record` 的投影映射、以及批缓冲 +
owner fence + 线程锁。前三层是**格式知识**，第四层是**运行时状态**。

第 1 期的 §7.1 表格已经点名 `features.jsonl` 属于「有结构、多消费方、无策略参数 →
storage 转」那一档。真正逼着它搬家的是域隔离：产物要从 step 根挪进 `{step}/features/`，
而 `_JsonlBuffer._path` 是当前唯一知道这条路径的地方——它不搬，第 4 期就没有可切换的开关。

## 方案详情

### 1. 切在哪：格式知识进包，运行时状态留下

| 现在在 `store.py` | 去向 | 判据 |
|------|------|------|
| `_path()` 目录拼装 | **进包** | 落盘布局，第 4 期要整体切换 |
| `features.jsonl` / `facts.jsonl` 文件名 | **进包** | 同上 |
| `_serialize/_deserialize_detection`、`_feature_to_record`、`_record_to_feature` | **进包** | 磁盘 record 的形状 = 内容格式（§7.1 第二档） |
| JSONL 行框定：一行一 JSON、坏行跳过、BOM 容忍、`ensure_ascii=False` | **进包** | 同上 |
| `os.replace` 原子换名 | **进包** | 「整体替换怎么才不产生中间态」是落盘机制 |
| 批缓冲 / `batch_size` / `_buffers` | 留 `store.py` | 节流策略，且是状态（D4 禁止） |
| owner fence / `open_fresh` / `_lock` | 留 `store.py` | run 身份与并发协调，包 docstring 边界清单已列名 |
| `try/except` 吞 IO 异常记 warning | 留 `store.py` | 是**策略**不是格式，见 §3 |
| `replace_segments` 的 producer 过滤合并 | 留 `store.py` | 业务判断（R3） |
| `fact_from_json` / `to_json` | 留 `inference/types.py` | leaf 不得 import `app.services.*`，见 §2 |

对外 6 个成员，两份产物各一组：

```python
append_features(task_id, step_id, features: Sequence[FrameFeature]) -> None
load_features(task_id, step_id) -> List[FrameFeature]        # 按 ts 升序
remove_features(task_id, step_id) -> bool

append_facts(task_id, step_id, records: Sequence[Mapping]) -> None
load_facts(task_id, step_id) -> List[Dict]                   # 保持落盘顺序
replace_facts(task_id, step_id, records: Sequence[Mapping]) -> None
```

**本模块零 `Path` 出口**。R2 允许「载荷字节的取用位置照出 `Path`」，但那是给
`/media/*` 转发 mp4 那类场景的；JSONL 没有任何消费方需要自己开文件，出 `Path` 只会
把布局又复制一份出去。

**没有 `remove_facts`**：`FactLedger` 继承来的 `open_fresh` 全仓零调用点（`grep`
确认只有 `feature_store.open_fresh` 一处）。按第 1 期的准入判据 2（「几个包会因它变了
而出错？< 2 不进」），零消费方的能力不进包——这正是第 1 期删掉 `client_manager`
re-export 的同一条理由。第 4 期顺带把 `open_fresh` 从基类降到 `FeatureStore`。

### 2. 两份产物的对外货币不同，是 leaf 身份逼出来的

| 产物 | 进出类型 | 为什么 |
|------|------|------|
| `features.jsonl` | `FrameFeature` | 它在 `app.domain.detection`，L0 共享契约，leaf 可以依赖 |
| `facts.jsonl` | `Dict[str, Any]` | `EventFact` / `SegmentFact` 在 `app.services.inference.types`，**leaf 不得 import 任何 `app.services.*`** |

这个不对称不是妥协出来的半成品。事实类型的 docstring 自陈是「inference 私有数据形状」，
它本就不该被一个所有人的共同下游认识；而 `to_json` / `fact_from_json` 本就与类型同居
（`types.py` 里带 `type` 判别字段的那对函数）。缝切在 dict 上，两边各自完整：
**本模块做行框定 + JSON 编解码，`inference` 做 record ↔ 事实对象的映射。**

被否掉的两条替代路线：

| 方案 | 否掉的理由 |
|------|------|
| 把 `EventFact` / `SegmentFact` 搬到 `app/domain/` | 要改 6 个文件的 import，本期明令不动调用点；且它们确实只有 inference 消费，搬过去是为迁就工具而改分层 |
| 本期不迁 facts，只迁 features | 两份产物同域同生命周期，只迁一半会让 `{step}/features/` 这条路径有两个主人——正是这次改造要消掉的东西 |

若哪天事实升格为 `app.domain` 契约，本模块可顺势把 codec 收进来，只换 `load_facts`
的返回类型标注。

### 3. 错误语义：内容坏了逐行隔离，环境坏了原样抛

这是相对 `store.py` 现状的**行为翻转**，也是本期唯一需要第 4 期显式接住的东西。

| 失败 | 本模块 | 理由 |
|------|------|------|
| 单行 JSON 解析失败 | 跳过 + `warning` | JSONL 逐行独立是格式性质，一行坏了不该让其余几万帧陪葬。这是格式事实 |
| 合法 JSON 但不是对象（`123` / `[1,2]`） | 同上 | 本域每行按契约是一条 record；放行只会把 `AttributeError` 推到下游才炸 |
| record 形状不对（缺 `conf`、`features` 不是对象） | 同上（仅 `load_features`） | 同一条判据的延伸。`store.py` 现在是整个循环一个 `try`，一条坏 record 会**丢掉它之后的全部帧** |
| 打不开 / 写不进 / 建不了目录 | **`OSError` 原样抛** | 「落盘失败要不要打断主链路」是调用方的判断 |

`store.py` 现在一律吞掉记 warning。但那个策略对两个调用方并不同时成立：online 写回
确实该 best-effort（丢几帧特征不值得打断推理），而离线 `replace_segments` 必须让失败
可见——吞掉的话「幂等替换成功」就是假的。本模块给不出对两者都对的答案，所以不给
（R3「不定义错误语义」）。第 4 期在 `_JsonlBuffer._write` 外层保留现有 `try/except`
即可维持 online 侧行为不变。

### 4. 三处口径明确 / 收紧，均已用例钉死

- **空批语义两侧相反**：`append_*([])` 是 no-op 且**不建目录**（否则 `tasks.ids()` 会
  列出一个从没写过东西的 step）；`replace_facts([])` **写出空文件**（= 清空）。区别在
  调用方意图：追加零条 = 没事发生，替换成零条 = 这份产物现在什么都没有。
- **临时件加前导点**：`facts.jsonl.tmp` → `.facts.jsonl.tmp`，与目标同目录（W1 同卷）。
  前导点标记「不是产物」，与第 1 期 §3 定的 tmp 约定对齐。固定名不带 nonce：同一目标
  的并发由调用方序列化（W6），崩溃残留的那个下次写时被覆盖，不会越攒越多。
- **facts 读编码 `utf-8` → `utf-8-sig`**：与 features 侧统一。纯放宽（BOM-less 文件
  解析不变），Windows 上手写/另存的 JSONL 带 BOM 时不再整文件读失败。

### 5. 回填第 1 期 §7 规范：写侧缺一条路线，读侧缺一条错误语义

feature 是验证 §7 草稿的第二个域文件。两条实质补充：

**§7.2 需要第三条路线 C（overwrite）**。现有 A（commit）设想的是「调用方在 tmp 目录
把产物造出来 → storage 换名」，为 ffmpeg 产段那种外部工具场景写的；B（append）不含
替换。而 `replace_facts` 两者都不是——**调用方给内存对象，storage 序列化后原子换名**：

```text
路线 C  overwrite（整体替换，内容由 storage 序列化）
  ① 编码   整批先编码完（失败时盘上一个字节没动）
  ② 写 tmp  与目标同目录的 .{name}.tmp
  ③ rename  os.replace 原子换名；任一步失败 → 删 tmp、不换名、原异常抛出
```

与 A 的分界：**产物字节是谁造的**。外部工具造 → A（storage 只换名，且可能要做位置
相关修补）；storage 自己从内存对象序列化 → C。`replace_facts` 走 C，W1/W2/W4 照样适用，
W3（位置相关修补）不适用。

**§7.1 应补一条 R6：IO 失败原样抛，不吞、不包成领域异常。** R3 只说了「不出领域异常」，
没说该抛还是该吞；本期定的口径见 §3。

其余条文本期全部验证通过、无需修改：R1（出 `FrameFeature`/dict）、R2（零 `Path` 出口）、
R3（producer 过滤留外面）、R5（一次调用一次扫盘）、W5/W6、T1（codec 往返）、
D1（依赖上界 `app.domain` + stdlib）、L4/L5。R4（过滤参数）、T2（`ts_to_us` 往返）、
L1/L2/L3（`SegmentRef` 那套）本域无对应形态，留给 `hls.py` 验证。

**新发现一个命名坑，已写进模块头**：模块叫 `feature`，域目录叫 `features`。前者对齐
「一域一模块」的命名习惯，后者是盘上既成事实。`_DOMAIN = "feature"` 写错会被
`_root.DOMAINS` 白名单当场 `ValueError`——第 1 期那条白名单的第一个真实用例。

### 6. 门禁：逐模块导入预算 + 覆盖检查

补第 1 期记录里点名的洞（「`BUDGET` 只登记包名，而标记型 `__init__` 不加载任何域文件，
故『往包里塞 cv2 会先红』并不成立」）：

```python
"app.services.storage":         (set(), 0.20),   # 标记型 __init__
"app.services.storage._root":   (set(), 0.20),   # stdlib only
"app.services.storage.tasks":   (set(), 0.20),   # stdlib only
"app.services.storage.feature": (set(), 0.40),   # 吃 app.domain（numpy）
```

采用「逐模块登记」而非 `pkgutil.walk_packages` 遍历：各域文件的依赖上界本就不同
（`_root`/`tasks` stdlib-only、`feature` 允许 numpy），遍历只能给一个统一上限。
代价是新域文件可能漏登记，故加一条 `test_storage_modules_are_all_budgeted`——扫包内
全部 `.py`，少一条就红。**新增域文件时登记预算成为一次显式决策**，与 `DOMAINS`
白名单同款思路。

实测：`app.services.storage.feature` 冷进程 0.091 s / 241 模块 / 重依赖为空
（numpy 随 `Detection.mask` 的类型标注进来，D1 允许的唯一一档），上限 0.40 有 4× 余量。

### 7. 第 4 期迁移指引（本期不做，记在这里免得届时重新推导）

`_JsonlBuffer` 目前缓冲的是**序列化后的字符串**；序列化归包之后，它要改成缓冲
**内存对象**（`FrameFeature` / fact record），落盘时把整批交给
`storage.feature.append_*`。这与 W5「一次调用写完调用方给的一批，包内不攒批」正好对上。
四处要动：

1. `_enqueue` / `_write` 的 `lines: List[str]` → `records: List[Any]`；`json.dumps` 从
   `FeatureStore.append` / `FactLedger.append` 里删掉。
2. `_write` 改为调 `append_features` / `append_facts`，**外层保留 `try/except` 吞异常**
   （见 §3，否则 online 写回从 best-effort 变成会抛）。
3. `open_fresh` 的 `path.unlink()` → `remove_features`；同时把 `open_fresh` 从
   `_JsonlBuffer` 降到 `FeatureStore`（`FactLedger` 侧零消费方）。
4. `replace_segments` 保留读—过滤—合并三步（`load_facts` → 过滤 → `replace_facts`），
   删掉自己那段 tmp + `os.replace`。注意它现在全程持 `_lock` 做 read-modify-write，
   拆成两次包调用后锁的范围不变，但**中间多了一次跨函数边界**——锁仍在调用方手里，
   语义不变。

## 变更效果

| 维度 | 变更前 | 变更后（第 4 期才实际生效） |
|------|------|------|
| features 域落盘知识的主人 | `inference/feature/store.py` 一处，与批缓冲/owner fence 混居 | `storage/feature.py`，leaf 门禁锁死；`store.py` 只剩状态与策略 |
| 产物位置 | `{step}/features.jsonl`、`{step}/facts.jsonl`（平铺） | `{step}/features/` 域子目录下 |
| `FrameFeature ↔ record` codec | 无往返测试 | 7 条往返用例，含「投影有损是契约」那条 |
| IO 失败 | 一律吞掉记 warning | 包内抛 `OSError`，吞不吞由调用方定 |
| facts 整体替换的临时件 | `facts.jsonl.tmp`（无前导点，看着像产物） | `.facts.jsonl.tmp` |
| facts 读编码 | `utf-8`（带 BOM 则整文件读失败） | `utf-8-sig`，与 features 侧统一 |
| 导入预算门禁 | 只登记包名，盖不住域文件 | 逐模块登记 + 覆盖检查，漏登记就红 |
| `import app.services.storage.feature` | —（不存在） | 0.091 s / 241 模块 / 重依赖为空 |

**自测结果**

| 项 | 结果 |
|------|------|
| `tests/test_storage_feature.py`（新增） | 40 passed —— codec 往返 7 条（含有损投影、空 source 保留、np 标量、缺分辨率、int ts）、features 读写 14 条（落位在域目录/空批不建目录/坏行隔离/BOM/IO 抛错/task-step 隔离）、`remove_features` 4 条、facts 读写 6 条、`replace_facts` 7 条（含 `os.replace` 失败保留旧文件、临时件与目标同目录）、与 `tasks` 域接缝 2 条 |
| `tests/test_import_hygiene.py` | 13 passed（原 9 + 逐模块预算 3 项 + 覆盖检查 1 条） |
| 全量 `pytest tests/` | **529 passed**，零 failed（第 1 期后为 485，本期纯增量 +44） |
| 既有测试文件 | 只碰 `test_import_hygiene.py`，diff 为纯新增；无任何既有用例被改或删 |
| 运行时行为 | 未验证也无需验证——本期不改任何生产调用路径 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------|------|------|
| **本模块无生产消费方** | 与第 1 期同款：新代码只有单测在跑 | 第 4 期迁移是价值兑现点。分期的自觉代价 |
| **IO 错误语义翻转，第 4 期不接就是行为变更** | `store.py` 现在吞 IO 异常；直接换成 `append_features` 而不包 `try/except`，online 写回会从 best-effort 变成向推理写回口抛 `OSError` | 已写进上方 §7 迁移指引第 2 条。第 4 期 PR 描述里要点名这一条 |
| **`facts.jsonl` 的货币是 dict 而非事实对象** | 调用方多一步 `fact_from_json` 映射；与 features 侧的 `FrameFeature` 不对称，读代码的人会问为什么 | 已在模块 docstring 写明是 leaf 门禁的结构性后果。事实类型若升格 `app.domain` 可消解，但那是独立立项，本期不推 |
| **`replace_segments` 拆成两次包调用后锁范围不变，但边界变多** | 锁仍在 `store.py` 手里，语义不变；风险在于后人误以为 `load_facts` / `replace_facts` 自带原子性 | `replace_facts` docstring 已写死「跨调用不保证原子，同一目标的并发由调用方序列化」。第 4 期迁移时在 `replace_segments` 内再注一句 |
| **§7 规范仍是草稿** | 本期补了路线 C 与 R6，但 `hls` 域的形态（`SegmentRef`、`parse_*`、commit 路线的位置相关修补）一条未被验证过 | 落 `hls.py` 时继续回填；三个域文件齐了再把摘要同步进 `__init__.py` docstring |
| **落盘结构切换仍是 breaking 变更** | 同第 1 期：第 4 期切换那一刻现有 `database/` 全部读不到 | 立场与处理不变，见 [20260909_STORAGE_LAYER_BASE](20260909_STORAGE_LAYER_BASE.md) 的同名条目。**清空时机需人拍板** |
| **`hls.py` / `lab.py` 未落地** | 第 4 期迁移需要三个域文件齐备才能一次性替换调用点 | `hls.py` 设计已定（见 [HLS_DOMAIN](20260909_STORAGE_HLS_DOMAIN.md)，14 个成员，待实现）；`lab.py` 未开工（小，只管导出临时件） |
