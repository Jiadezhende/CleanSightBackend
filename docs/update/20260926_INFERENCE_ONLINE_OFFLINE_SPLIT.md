# inference 包拆成 online/ + offline/ + 共享层（纯搬迁）

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

`app/services/inference/` 的在线链路整体移入 `online/`，`offline/` 原位不动，两段共用的 `config.py` / `stage_factory.py` 留在顶层。
只改路径，不改行为；推翻 [20260903_PACKAGE_LAYOUT_SPEC](20260903_PACKAGE_LAYOUT_SPEC.md)「inference 子包不做物理重组」一条。

## 变更背景

- **现状**：`detection/`、`temporal/`、`visualization/`、`offline/` 与 `manager` / `naming` / `types` / `config` / `stage_factory` 平铺在同一层，看不出哪些模块属于实时链路、哪些属于离线、哪些两边共用。
- **触发来源**：要在 `offline/` 里新增离线作业服务（下一批）。评审要求先按链路划分，公共模块与 `online/`、`offline/` 同级，方便两边共享。
- **推翻的旧约定**：20260903 规范认为「加中间目录只让深路径变长」。现在离线侧也要有自己的服务和单例，按链路划分已经有了实质收益。

## 方案详情

### 全景

```text
app/services/inference/
  __init__.py        lifespan()（单例路径改为 .online.instance）
  config.py          共享：stage 配置
  stage_factory.py   共享：Detector / Operator / OfflineSegmenter 工厂
  online/            ← detection/ temporal/ visualization/ manager instance naming types 整体 git mv 进来
  offline/           原位；__init__ 改为标记型（去掉 runner / segmenter 的 re-export）
```

归属依据：online 与 offline 运行期互不 import，只有 `stage_factory.py` 里一处 TYPE_CHECKING。`naming` / `types` 只有在线链路和 `routers/task.py` 用；`config` / `stage_factory` 两边都用。

### 1. 路径改写

| 类别 | 改法 |
|------|------|
| 绝对 import / docstring / markdown 链接 | `app.services.inference.{detection,temporal,visualization,manager,instance,naming,types}` → `app.services.inference.online.*`（app / tests / skills / DEVELOPMENT.md） |
| `online/manager.py`、`online/naming.py` 引共享层 | `from .config` / `from .stage_factory` → 绝对路径（跨包一律绝对，见 `test_intra_package_relative_cross_package_absolute`） |
| [`run_control.py`](../../app/services/run_control.py) | `.inference.online.instance` / `.inference.online.temporal` |
| [`config/inference_config.yaml`](../../config/inference_config.yaml) | detection / temporal 的 9 处 `class:` 加 `online.`；offline 的 `class:` 不变 |
| [`tests/test_import_hygiene.py`](../../tests/test_import_hygiene.py) | `SINGLETONS["inference_manager"]` 与 alarm_sink 例外路径跟着改；`BUDGET` 新增 `inference.online` / `inference.offline` 两条 |

### 2. 保留项

- **不留兼容 shim**：旧路径上做 re-export 违反包的零 re-export 约定；SINGLETONS 门禁按模块名精确匹配，留 shim 还会让它漏检。
- `python -m app.services.inference.offline.cli` 入口路径不变。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| yaml 中 11 个 `class:` 逐个 importlib 加载 | 全部 ok |
| 全量 `pytest tests/` | 924 passed（原 922，+2 条新 BUDGET） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/` 里的路径与 SERVICE_INFERENCE 包结构表仍是旧的 | 读 KB 会拿到旧路径 | 下次 KB 融合时按本记录改 |
| 分支外若有代码仍引用旧路径（如未合并的 feature 分支） | 合并后 ImportError | 合并时按上面「路径改写」表替换 |
