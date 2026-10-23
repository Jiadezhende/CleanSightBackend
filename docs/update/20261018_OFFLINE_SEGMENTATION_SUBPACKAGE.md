# 离线动作分割收进 `offline/segmentation/` 子包，与规则判定 `offline/compliance/` 并列

> **变更状态**：生效中（2026-10-18）
> **知识库**：待沉淀

## 概述

`offline/` 下平铺的分割实现（`segmenter.py` / `runner.py` / `materials.py` / `impl/`）整体移入 `offline/segmentation/`，
与规则判定 `offline/compliance/` 并列；`offline/` 根只留共用层（作业服务 `service` / `instance`、子进程入口 `cli`）。
纯搬家，行为不变；**YAML `offline.class` 路径随之改变**，没有旧路径兼容。

## 变更背景

- **现状**：离线包新增规则判定后，分割实现仍平铺在 `offline/` 根，与作业服务、CLI 混在一层，看不出哪些属于分割。
- **触发来源**：人工评审要求分割与判定两个功能模块分清。

## 方案详情

### 全景

```text
app/services/inference/offline/
  __init__.py
  service.py / instance.py     离线作业服务（共用）
  cli.py                       run = 分割后接判定；check = 只重跑判定（共用，唯一同时 import 两边的地方）
  segmentation/                动作分割  ← segmenter.py / runner.py / materials.py / impl/ 原样移入
  compliance/                  规则判定
```

| 改动 | 落在哪 |
|------|--------|
| 文件移动（`git mv`） | `offline/{segmenter,runner,materials}.py`、`offline/impl/` → `offline/segmentation/` |
| 新增包标记 | `offline/segmentation/__init__.py` |
| 引用改写 | `stage_factory`（`OfflineSegmenter` 类型）、`offline/cli.py` / `offline/service.py`、6 个策略包对框架契约的 import 与 docstring YAML、`config/inference_config.yaml` 的 `offline.class`、online `impl/` 注释、README、`infer-workflow` skill |
| 门禁 | `test_import_hygiene`：`OFFLINE_IMPL_DIR` 指向新位置，策略包白名单里的框架契约改为 `offline.segmentation.segmenter`；新增 `test_offline_impl_units_found`（目录挪了门禁零用例会静默变绿）；分割侧不得 import compliance 的门禁改为扫 `offline/segmentation/` |
| 测试 | 各策略包测试、`test_offline_impl_catalog`、`test_offline_pipeline`、`doubles` 的 import 路径 |

### 保留项

- 策略包内部、Runner / 作业服务行为、物料目录（`app/data/<模型目录>/`）不动。
- `offline/cli.py` 模块路径不动：作业服务起子进程的命令 `python -m app.services.inference.offline.cli run` 不变。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 1171 passed |
| 旧路径残留 | `app/ tests/ config/ README.md .claude/skills` 内 grep `offline.impl` / `offline/impl` / `offline.segmenter` / `offline.runner` / `offline.materials` 无残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 仓库外的 `inference_config.yaml` 副本（部署机手改过的）仍写旧 `offline.class` | 提交离线作业时类加载失败（fail-fast，不静默） | 部署时同步仓库 YAML；或手改为 `app.services.inference.offline.segmentation.impl.…` |
| KB 仍写旧路径 | 读 KB 的人找不到文件 | 下次 KB 融合时按本记录更新 |
