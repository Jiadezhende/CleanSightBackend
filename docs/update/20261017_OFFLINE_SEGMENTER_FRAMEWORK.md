# 离线策略框架：物料清单与启动预检，策略包隔离门禁，框架测试只用替身

> **变更状态**：生效中（2026-10-17）
> **知识库**：待沉淀

## 概述

离线分割策略重构（一个策略一个自包含包、历史策略全部保留）的第一批，只动框架：`OfflineSegmenter` 加 `materials()`，
新增 `cli materials` 物料核对与作业服务启动预检；导入门禁新增「策略包只许 import 自身与框架契约」；框架测试不再引用真实策略。
现有策略行为不变。

## 变更背景

- **现状**：离线每个 stage 只激活一个策略，但研究需要长期保留历史策略做对照。[impl/clean.py](../../app/services/inference/offline/impl/clean.py)
  里 4 个历史策略共用基类和特征函数，改一处影响全部；物料缺失要等第一个离线作业失败才发现；框架测试拿真实历史策略当夹具，
  拆策略会连带改框架测试。
- **后续批次**：历史策略逐个迁成自包含包（v2 系三个 → nodep GRU / mstcn2-bundle → 迁调用点与物料目录 → 删 clean.py）。

## 方案详情

### 全景

```text
OfflineSegmenter.materials() -> Sequence[Path]       默认 ()；只拼路径，不读盘、不 import torch
  └ offline/materials.check_materials(config)        逐 stage 构造激活策略 → [MaterialStatus(stage, producer, path, exists)]
       ├ cli materials                               打印 JSON；有缺失退出码 1（部署核对）
       └ OfflineJobService.start() 预检               缺失逐条 WARNING，预检本身出错也只告警；不阻断启动
test_import_hygiene::test_offline_impl_imports_only_framework_contract
       impl/ 下每个顶层包 / 单文件模块：只许 import 自身 + offline.segmenter / inference.resample / app.types
```

| 部件 | 落在哪 |
|------|--------|
| `materials()` | [`offline/segmenter.py`](../../app/services/inference/offline/segmenter.py) |
| 核对逻辑 | [`offline/materials.py`](../../app/services/inference/offline/materials.py) |
| CLI 子命令 / 启动预检 | [`cli._materials`](../../app/services/inference/offline/cli.py)、[`OfflineJobService._preflight_materials`](../../app/services/inference/offline/service.py) |
| mstcn2-role-embed 的清单 | `temporal.pt`、`hand_state.npz`、骨干 `hubconf.py` 与权重 |
| 门禁 | [`tests/test_import_hygiene.py`](../../tests/test_import_hygiene.py) `OFFLINE_IMPL_ALLOWED` |
| 测试拆分 | 真实历史策略的用例挪到 `tests/test_offline_clean_legacy.py`（随 clean.py 一起删）；框架补 `ProbsSegmenter` / `MaterialSegmenter` 替身 |

### 关键约定

- **门禁为什么不复用 `LAYER_PACKAGES`**：它按包前缀放行，`impl` 的自身前缀会把兄弟策略包一起放行；这里对每个顶层包单独套白名单。
- **启动预检不阻断**：物料缺失只影响该 stage 的离线作业，在线不受影响，所以只告警。

## 变更效果

| 项 | 结果 |
|----|------|
| 本机 `python -m app.services.inference.offline.cli materials` | 列出 step 2 激活策略的 4 个物料，全部存在，退出码 0 |
| 门禁自测 | 临时在 role-embed 包里 import `impl.clean` → 门禁红；还原后绿 |
| 新增用例 | 逐 stage 列清单、无 offline / 无物料的策略为空、CLI 退出码、启动预检告警但照常启动、role-embed 清单、label_probs 旁路写盘（替身） |
| 全量 `pytest tests/` | 1084 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 历史策略（clean.py）还没实现 `materials()` | 它们被激活时预检看不到缺失 | 迁成自包含包时各自实现 |
