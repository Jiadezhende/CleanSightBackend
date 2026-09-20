---
name: kb-merge
description: "Merge pending docs/update/ increments into the CleanSightBackend knowledge base (docs/kb/). Human-initiated batch maintenance: collect records marked 待沉淀, reconcile them against current code, fold conclusions into the right KB file, bump metadata dates, update INDEX, then flip each record to 已沉淀. Use when: 融合知识库、更新知识库、KB维护、沉淀待沉淀记录、把update并入KB、整理docs/update、知识库融合、kb merge、merge updates into kb、sync knowledge base."
---

# KB 融合 — update 增量并入知识库

日常开发**只写 `docs/update/`**（一个开发任务一份），KB 不随手改。本 skill 是那个「人主动发起」的批量融合流程。

验收标准（可信来源顺序、三行元信息、内容粒度、更新时间规则）以 [KB_MAINTENANCE.md](../../../docs/kb/KB_MAINTENANCE.md) 为准，本文只讲**怎么做**，不复制规范。

## Step 1 — 划定本批范围（看状态轴，不是看日期）

每份 update 顶部有两条状态轴（格式见 [_TEMPLATE.md](../../../docs/update/_TEMPLATE.md)），**知识库轴就是欠债清单**：

```bash
grep -l '待沉淀' docs/update/*.md           # 本批范围 = 尚未进 KB 的记录
head -4 docs/kb/INDEX.md                    # 交叉校验：晚于此日期的是否都已标待沉淀
```

- **只沉淀「变更状态 = 生效中」的内容**。`进行中` 跳过；`提案` 不是事实，不进 KB。
- **`已回退` 要反向处理**：不但不沉淀，还要回 KB 删掉对应的失效结论——这是唯一会让 KB **变短**的情况，容易漏。
- 日期只作交叉校验，别拿它当判据：比 INDEX 日期新的文件里可能混着提案，靠日期挑会把没落地的东西并进 KB。

把清单列给用户确认范围再动手。

## Step 2 — 按主题成链，不逐份并入

**最容易出错的一步。** 同一主题往往有多份 update 构成演进链，后者会**推翻或修正**前者（典型：`HLS_SEGMENT_TIMESCALE_FIX` → `HLS_WALLCLOCK_TIMELINE_REQUIREMENTS` → `HLS_TIMELINE_INDEX` → `FRAME_TRACKER_SIDECAR` → `FRAME_TRACKER_BOUNDARY_FIX`，中途还换过方案）。

- 先按主题分组、组内按日期排序，**读完整条链**再决定写什么 —— 逐份并入会把中途被废弃的方案当成事实写进 KB。
- 只有链末状态进 KB；中途的方案演进过程留在 update 里，KB 不记录"曾经怎么做"。
- 链中某份标了 `已回退`，说明该方案被推翻——检查它之前是否已污染 KB，是则一并清理。

## Step 3 — 拿当前代码复核

update 是**写入当时的快照**，可能已被后续改动推翻。每条要写进 KB 的结论都回代码验一遍（可信来源顺序见 KB_MAINTENANCE：代码 > 配置 > 测试 > 旧 docs）。

验不上的三种处理：结论已过期 → 按代码现状写；代码找不到依据 → 标「待核验」或不写；与 KB 现有内容冲突 → 以代码为准改写 KB，并在回复里点出这处冲突（属 P1，要报）。

## Step 4 — 判归属，写入

按主题定落点，KB 27 份文件的分工见 [INDEX.md](../../../docs/kb/INDEX.md) 各分组说明。惯例：

| update 主题 | 落点 |
|---|---|
| 某个服务内部实现 | `SERVICE_<域>.md` |
| 跨服务的工程设计/不变式 | `DESIGN_<主题>.md` |
| 业务概念、检测标准、生命周期 | `BUSINESS_*.md` |
| 组件关系、数据流、API 接线、存储分层 | `ARCHITECTURE_*.md` |
| 对外端点契约（请求响应 schema） | **不进 KB** → `docs/api/`，那是端点契约真源 |

写入时：

- KB 写**稳定结论**（职责边界、数据流、状态归属、约束），不抄 update 的改动过程与动机；不复制大段代码。
- 每个被改文件顶部 `更新时间` 改成本批日期，**同批统一同一天**。
- 新建 KB 文件必须加进 [INDEX.md](../../../docs/kb/INDEX.md) 对应分组（一句话说明），必要时进「推荐阅读路径」。
- 最后改 INDEX 自己的 `更新时间` —— 它是下次融合的判定基准，漏改会导致本批被重复融合。

## Step 5 — 回填状态轴（闭环，漏了下次会重复融合）

本批每份 update 的「知识库」轴由 `待沉淀` 改成 `已沉淀 → [目标kb文件](../kb/XXX.md)(YYYY-MM-DD)`，此后它变为纯历史，下次 `grep 待沉淀` 自动跳过。

文件**留在原地不归档** —— 状态轴已经区分了新旧，不靠移动文件表达。

```bash
grep -l '待沉淀' docs/update/*.md    # 本批做完后，应只剩 _TEMPLATE.md 和不该沉淀的
```

## Step 6 — 自检

```bash
grep -h '^> 更新时间：' docs/kb/*.md | sort | uniq -c   # 本批文件是否都是同一天
find docs/kb -maxdepth 1 -name '*.md' | sort            # 平铺，无子目录
grep -o '\[.*\](\([A-Z_]*\.md\))' docs/kb/INDEX.md      # INDEX 链接是否都存在
```

外加人工过一遍：新增文件已进 INDEX / INDEX 自身日期已改 / 无提案被当事实写入 / 本批状态轴已回填。

## 输出格式

融合完汇报：**本批并了哪几份 → 落到哪些 KB 文件**（一行一条）、**发现的 update 与代码冲突**（P1，必报）、**因 `已回退` 而从 KB 删掉的结论**（P1，必报）、**标了待核验的条目**。中途被废弃的方案、措辞调整这类不用报。
