# `app/storage/inference/` 子包：三份推理产物进数据层，域目录从 `features/` 改名

> **变更状态**：进行中（2026-09-21）　<!-- 域已落地且测绿，但零调用点；生产写侧仍走 inference/feature/store.py 的平铺落盘 -->
> **知识库**：待沉淀
>
> <!-- 推理域数据层接线的第 2 步。第 1 步（事实契约升格）见 20260921_INFERENCE_FACT_CONTRACT.md -->

## 概述

`app/storage/feature.py` 扩成 `app/storage/inference/` 子包，收编 `features.jsonl`、
`facts.jsonl`、离线调试件三份产物；域目录名从 `features/` 改成 `inference/`。对外 6 个成员，
**零调用点**——运行时行为不变。全量 857 passed（基线 827）。

## 变更背景

### 现状 / 痛点

`{task}/{step}/` 下有两份推理产物：`features.jsonl`（L1 目标检测，每帧一行）与
`facts.jsonl`（L3 时序分析，每条一行）。前者的落盘能力 2026-09-09 已进数据层
（`app/storage/feature.py`），后者**刻意没迁**：`EventFact` / `SegmentFact` 当时住在
`app/services/inference/types.py`，数据层的依赖白名单不许 import 服务层，货币只能退成
`dict`，与 features 侧收发 `FrameFeature` 不对称。于是一份产物的路径知识劈成两处。

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | 一个域只管得了两份产物中的一份 | 结构性阻塞：facts 的路径与格式知识留在服务层 |
| #2 | 域目录叫 `features/`，里面要放 `facts.jsonl` | 名实不符；再放调试件更甚 |
| #3 | `offline_inference_result.json` 落在 step 根 | 违反「step 根下只有域目录、没有文件」，且绕过定位收口（L5） |

### 承接

第 1 步已把 `EventFact` / `SegmentFact` 升格到 `app/domain/fact.py`（产出者身份归一到
`producer`、`ts` 必填、`to_json` 剥离），#1 的障碍因此消除。本批是第 2 步。

## 方案详情

### 全景：三份产物、两个产出层、三条写路线

```text
L1 目标检测 ──▶ features.jsonl       FrameFeature   路线 B 追加      _detection.py
L3 时序分析 ──▶ facts.jsonl          Fact           路线 C 原子替换  _temporal.py
离线策略调试 ─▶ offline_debug.json   Mapping        路线 C 原子替换  _temporal.py
                     │
                     └── 共用：_layout（域根 + 文件名 + 整域删除）、_jsonl（行框定 + 原子写）
```

| 步骤 / 部件 | 落在哪 | 详见 |
|---|---|---|
| 域名白名单换名 | `app/storage/_root.py` | §1 |
| 域根、文件名、整域删除 | `app/storage/inference/_layout.py` | §2 |
| 行框定 + 文本原子落盘 | `app/storage/inference/_jsonl.py` | §3 |
| features 的 codec 与读写 | `app/storage/inference/_detection.py` | §4 |
| facts / debug 的 codec 与读写 | `app/storage/inference/_temporal.py` | §5 |
| facade | `app/storage/inference/__init__.py` | §6 |

### 方案选型：为什么是子包而不是单文件

| 方案 | 代价 / 影响面 | 结论 |
|---|---|---|
| A（采用）子包，按**产出层**分两个实现模块 | 5 个文件替 1 个 | 两份产物有两对独立 codec、两条写路线、两个生命周期，这才是切包的判据 |
| B 单文件 `inference.py` 扩写 | 改动最小 | 否：约 360 行里塞两对逆运算，且第三份产物进来还要再拆一次 |
| C 两个**平级域**（`{step}/detection/` + `{step}/temporal/`） | 完全正交 | 否：两者共用同一条时间轴与同一套行框定，拆开后行框定要么复制、要么放跨域共享模块（破坏域隔离）；supersede 还要写两次 delete，漏一次就是旧 run 的分析结果活到新 run 里 |

### 1. `app/storage/_root.py` — `DOMAINS` 换名

`("hls", "features", "lab")` → `("hls", "inference", "lab")`。白名单是域名唯一真源，改完
`path(..., domain="features")` 当场 `ValueError`。

**此刻换名是零数据代价**：`{step}/features/` 至今没有生产写者（数据层 features 侧一直是死
代码），盘上只有老的平铺 `{step}/features.jsonl`。下一批接线上线后就不是了。

### 2. `_layout.py` — 域根、文件名、整域删除

照 `hls/_layout.py` 的形状：`_DOMAIN` 全文件只出现一次，`domain_dir()` 包内公开、facade 不
re-export。三个文件名常量 `FEATURES_NAME` / `FACTS_NAME` / `DEBUG_NAME`。

`delete(task, step)` 照抄 `hls/_write.py` 的实现与错误语义（`rmtree` 整域、返回此前是否存在、
**失败记 warning 返回 False 而不抛**）。它**取代了原来的 `delete_features`**：

```text
旧  delete_features(task, step)   只删 features.jsonl，同域 facts.jsonl 原样保留
新  delete(task, step)            三份产物一起没
```

语义变了而不只是改名：同 `(task, step)` 重启一次 run，旧 facts 是对旧 features 的分析结果，
留着即脏数据。一个域只留一个删除口径。

> 判断：`delete` 住在 `_layout` 是因为它跨两份产物、只需要 `domain_dir` 一个知识，两个产物
> 模块谁也收不下它。代价是一个「命名与定位」模块多出破坏性副作用。

### 3. `_jsonl.py` — 两份 JSONL 共用的行框定 + 路线 C 的原子写

`encode` / `decode` 从原 `feature.py` 原样搬（坏行逐行隔离、非对象行也算坏行、`utf-8-sig`
容忍 BOM）。新增 `write_atomic(path, text)`：同目录点开头 tmp → `os.replace`，失败删 tmp、不
换名、原异常上抛（W1 / W4）。facts 与 debug 共用它——两者都是「整批文本一次性替换」。

### 4. `_detection.py` — features.jsonl（路线 B）

codec 与 `append_features` / `read_features` 从原 `feature.py` 原样搬，行为零变化：投影有损
（mask/keypoints/metadata 不落）、空批不建目录、ts 升序是返回值契约、`OSError` 原样抛。

### 5. `_temporal.py` — facts.jsonl + offline_debug.json（路线 C）

新写。`type` 判别字段只活在 `_fact_to_record` / `_record_to_fact` 这对逆运算里，内存侧判别是
`isinstance`。两型**无损**落盘，故往返在全字段闭合。

三条落地时定下的语义：

```text
read_facts   不排序，原样返回落盘序
             —— 两型没有共同时间键（EventFact.ts 对 SegmentFact.start），层没有依据替调用方选
write_facts  整体替换，不读既有内容
             —— 盲写会吃掉别的 producer 的分段与所有 EventFact；调用方须 read → 合并 → write
空批         照写空文件、不删文件
             —— 「跑过、没分出任何段」与「根本没跑过」在盘上要能分开；删除是 delete 的事
```

`write_debug_result` 把 `offline_inference_result.json` 收编成 `{step}/inference/offline_debug.json`
（改名 + 换目录），消解背景 #3。只写不读——它是给人看的。

### 6. `__init__.py` — facade

照 `hls/__init__.py` 的骨架，re-export 6 个成员。docstring 里显式列出**刻意没有的成员**
（`append_facts` / `iter_features` / `*_path` / `read_debug_result`）与各自理由，免得后人当漏了。

### 7. 保留项（不改动）

- **零调用点**：`DetectionService._write_back_results`、`InferenceManager` 的
  `open_fresh` / `close` / `flush`、`OfflineRunner`、`offline/cli.py` 全部没动，生产路径仍走
  `app/services/inference/feature/store.py` 的平铺落盘。
- `app/services/inference/feature/` 子包与 `types.py` 里的旧两型原样留着，下一批才删。

## 变更效果

| 维度 | 变更前 | 变更后 |
|---|---|---|
| 数据层管得了的推理产物 | 1 份（features.jsonl） | 3 份 |
| 域目录 | `{step}/features/`（里面要放 facts） | `{step}/inference/` |
| 调试产物落位 | `{step}/offline_inference_result.json`（step 根） | `{step}/inference/offline_debug.json` |
| 域内删除口径 | `delete_features`（只删一份） | `delete`（整域，supersede 语义） |
| 运行时行为 | — | 无变化（零调用点） |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_storage_inference.py` | 53 passed（codec 往返 15、读写与落位 29〔含事务不变式 2〕、整域删除 5、产物互不干扰 2、`tasks` 接缝 2） |
| `tests/test_import_hygiene.py` | 35 passed（新增 5 条 `BUDGET`，`_temporal` 纯 stdlib、`_detection` 经 `FrameFeature` 吃 numpy） |
| 全量 `pytest tests/` | **857 passed**（基线 827；−27 旧用例 +53 新用例 +4 门禁条目） |
| 手工核对 | `grep '"features"' app/storage/` 零命中；写路线 B/C 的选择与理由写在各自 docstring |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|---|---|---|
| 本域仍是死代码 | 两份实现并存期变长（`storage/inference` 与 `feature/store.py`），改一份不会让另一份红 | 下一批（写侧迁移 + 删 `inference/feature/`）紧接着做，中间不插别的任务 |
| `write_facts` 盲写会吃掉别的 producer | 离线重跑丢事实 | 门禁抓不到，只能 docstring + review；下一批由 `OfflineRunner` 的 read → 合并 → write 兑现，配用例钉住 |
| `delete` 的破坏性副作用住在 `_layout` | 后人往 `_layout` 里加函数时可能误以为这是个纯定位模块 | docstring 已写明理由；`hls` 那边的对应物在 `_write.py`，两域不对称是已知代价 |
| 老的 `{step}/features/` 目录读侧不可见 | 无——该目录从没有过生产写者 | 无需处理 |
