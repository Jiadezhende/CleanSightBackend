# 文档治理：CLAUDE.md 瘦身、KB 写入权限收敛、新增 /kb-merge skill

> **变更状态**：生效中（2026-09-02）
> **知识库**：无需沉淀（本次改的是文档流程约定本身，结论已直接落在 CLAUDE.md / DEVELOPMENT.md / KB_MAINTENANCE.md / kb-merge skill；KB 记录系统事实，不记录流程约定）

## 概述

- **改了什么**：CLAUDE.md 硬规矩按使用频率重排——高频行为规范留下，主题性约定下沉 DEVELOPMENT.md；明确 `docs/kb/` 不由日常开发写入，把「怎么执行一次融合」抽成 `/kb-merge` skill。
- **为什么改**：CLAUDE.md 每轮都进上下文，7 条硬规矩里有 2 条是 DEVELOPMENT.md 的重复摘要、2 条只在新建检测点时用；同时「描述性内容改动同步进 docs/kb/」这句实际在授权随手改 KB，与「KB 由人主动发起维护」的真实约定相反。
- **影响面**：纯文档与 skill，无代码改动。

## 改动详情

### 1. `CLAUDE.md` — 硬规矩 7 条 → 6 条并按时序分组

下沉到 DEVELOPMENT.md：测试 factories 单一真源、跨服务解耦（两条本就是 DEVELOPMENT §2/§3 的重复摘要）、`class_name` 不归一化、`Detection`/`FrameDetections` 契约（新增 §5）、`/docs` 永久关闭（删除，`docs/api/README.md` 已有单一真源）。

保留并重写的：

| 旧 | 新 |
|---|---|
| 部署默认 dev，别擅自当 prod | 开发只跑 dev / test，不碰 prod——原文把 prod 写成"需授权才能选的档位"，易被读成问一句就能用 |

新增四条（动手前：先评估现有能力再设计；动手后：改动留档；汇报时：只上报 P0/P1、要决策就给决策依据）。

文档路由表拆出 `docs/api/` 独立一行（端点契约真源，此前与 KB 混在一行）。

### 2. `docs/DEVELOPMENT.md` — 承接下沉内容

新增 §5 检测点 / Workflow 契约。§1 文档纪律改写：update 粒度改为**一个开发任务一份**（原「每次提交」）、点明状态轴与 `/kb-merge`、KB 不随手改。

§1 提交前自检补一条：**逐个 `git add`，禁 `git add .` / `-A`**，中间产物 / 样本数据 / 模型权重 / 大体积文件不进版本库——大文件一旦入库，git 历史永久留存。

### 3. `.claude/skills/kb-merge/SKILL.md`（新增）— 融合流程

规范（验收标准）留 [KB_MAINTENANCE.md](../kb/KB_MAINTENANCE.md)，skill 只写流程：划范围 → 按主题成链 → 拿代码复核 → 判归属写入 → 回填状态轴 → 自检。

> **写作中修正的一处错误**：初版把「范围判定」写成比对 INDEX 更新时间与文件名日期，并新定了 `docs/update/archive/` 归档规则。实际上 [_TEMPLATE.md](_TEMPLATE.md) 早有「两条状态轴」机制且是活的（67 份中 64 份带轴、51 份已回填 `已沉淀`），`grep -l '待沉淀'` 才是判据。日期法会把提案类误判为待融合（`20260814_CLEAN_STAGE_MODEL_PROPOSAL` 即是）。archive 规则一并撤销——51 份已沉淀记录全留原地，说明设计上就靠状态轴而非移动文件区分新旧。

### 4. `docs/kb/KB_MAINTENANCE.md` — 补定位说明

顶部说明本文是验收标准、流程见 `/kb-merge`；按其自身规则更新日期至 2026-09-02。

## 保留项（不改动）

- `docs/update/` 存量 60+ 份不追溯归档。
- 积压的 6 份 `待沉淀` 记录本次不融合，留待人发起。
