# 推理模块按处理流程分层重组：抽象/impl 分离 + 每层隔离 + offline 预留

> **变更状态**：生效中（2026-06-30）
> **知识库**：已沉淀 → [kb/SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)(2026-07-21)
>
> 相关：[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（L1–L4 分层数据流来源）、[20260627_STREAM_OPERATOR_FRAMEWORK.md](20260627_STREAM_OPERATOR_FRAMEWORK.md)（Detector/Operator 命名来源）、[20260628_DATAMODEL_LAYERING.md](20260628_DATAMODEL_LAYERING.md)（数据模型分层前序）、[20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md](20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md)（offline 层一期实现）。

## 概述

- **改了什么**：把 `app/services/inference/` 的**文件组织**按处理流程归位——检测 / feature_store / 时序分析 / 可视化各成独立 package，层内**抽象 ABC 与 impl/引擎分文件**、**worker 与 pool 不混文件**；跨层基础设施（manager/config/naming/stage_factory/models）平铺顶层；具体检测任务保持内聚原地留 `workflows/`；末尾预留空 `offline/` 层。
- **为什么改**：内部数据流早已分层（L1→L4+Viz），但文件组织没跟上——抽象 ABC（`detector.py`/`operator.py`）与具体实现混在 `workflows/`，各层处理引擎散落在 `workers/`、根目录、`workflows/` 三处，且 worker/pool/渲染器挤在同一文件。
- **影响面**：纯结构重组，**运行时行为零变化**；229 单测全绿。YAML class 路径零改动（检测任务文件原地未动）。

## 目录结构（前 → 后）

| 旧位置 | 新位置 | 角色 |
|--------|--------|------|
| `core/manager.py` | `manager.py`（顶层） | 总编排 InferenceManager |
| `core/dispatcher.py` | `detection/dispatcher.py` | L1 取帧分组 |
| `workers/base.py` | `detection/pool.py` | L1 模型池 MultiModelWorkerPool |
| `core/service.py` | `detection/service.py` | L1 worker ModelWorkerService |
| `workflows/detector.py` | `detection/detector.py` | L1 抽象 Detector/YOLODetector |
| `store.py` | `feature/store.py` | L2 FeatureStore/FactLedger |
| `workflows/operator.py` | `temporal/operator.py` | L3/L4 抽象 Operator/AlignedFrame |
| `workers/temporal.py` | `temporal/actor.py` | L3/L4 worker ClientTemporalActor |
| `workflows/alarm_sink.py` | `temporal/alarm_sink.py` | L4 出口 persist_alarms |
| `workers/visualization.py` | `visualization/{worker,pool,visualizer}.py` | Viz 三分：拉取循环 / 线程管理 / 固定渲染器 |
| —— | `offline/__init__.py` | 离线段预留空包占位 |
| `workflows/{bubble,bending,mock,clean}.py` | 原地不动 | 可插拔检测任务（Det+Op 内聚单文件） |
| `config.py`/`naming.py`/`stage_factory.py`/`models.py` | 原地不动 | 顶层跨层基础设施 |

> 解散 `core/`、删空 `workers/`。`detection/` 自包含「取帧→模型池→worker 写回」整条 L1 链。

### 分层准则
- **每层处理流程隔离**：`detection/`、`feature/`、`temporal/`、`visualization/`、`offline/` 各成 package，一层一目录。
- **抽象/impl 分离 + worker/pool 不混**：抽象 ABC 独立成文件（`detection/detector.py`、`temporal/operator.py`）；worker（`service.py`/`actor.py`/`worker.py`）与 pool（`pool.py`）分文件；渲染器 `visualizer.py` 从 worker/pool 拆出；ABC 的具体实现留 `workflows/`，与抽象彻底分家。

## 兼容与护栏

- **公共 API 不变**：顶层 [`__init__.py`](../../app/services/inference/__init__.py) `__all__` 符号集保持不变，仅改 re-export 来源；各层包 `__init__.py` 转出本层符号。
- **外部引用**：[`app/services/ai.py`](../../app/services/ai.py) 改走包根 `from app.services.inference import InferenceManager`（1 行）；`naming.get_task_metric_map`、`models.FrameInference` 原地未动，`routers/task.py`、`client/queues.py` 零改动。
- **YAML class 路径**：[`config/inference_config.yaml`](../../config/inference_config.yaml) 8 处 `...workflows.bubble.BubbleDetector` 等全部继续有效，零改动。
- **测试**：moved 模块的深 import 与 `patch(...)` 字面量按新路径订正（含 `manager.client_manager`、`visualization.worker.logger`、`detection.dispatcher.logger`）。

## 验证

| 项 | 结果 |
|----|------|
| `python -c "import app.main"` | OK |
| 全量 `pytest tests/` | 229 passed |
| 残留旧路径扫描（`inference.core/workers/store`、`workflows.detector/operator/alarm_sink`） | 0 |

## 后续（不在本次）

- `offline/` 一期实现（OfflineSegmenter/Runner/CLI 等）按 [20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md](20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md) 独立推进。
- `/infer-workflow` skill 模板仍滞留旧三件套（analyzer/judge/data_models，前次 Operator 重构遗留），本次仅订正 detector 抽象路径，模板全量同步到 Operator 框架另列任务。
