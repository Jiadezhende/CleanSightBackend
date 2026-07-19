# 离线时序 baseline 接入：features.jsonl 到 SegmentFact/FactLedger 闭环

> **变更状态**：待提交 PR（2026-07-14）
> **知识库**：待沉淀
>
> 承接：已有离线入口定义了 `FeatureStore.load -> OfflineSegmenter -> FactLedger` 的一期骨架；本次补齐一个可运行 baseline、手动 worker 和针对性测试，便于后续接真实时序模型。

## 概述

- **改了什么**：新增离线时序模型通用接口、规则式 `brush_rule` baseline、主链路 `OfflineSegmenter` 包装、手动 worker，以及对应测试。
- **为什么改**：一期需求要求能够手动启动离线推理任务，读取 `features.jsonl`，产出有效行为时间线，并写出结构化结果。真实 MS-TCN / ASFormer / 其它 SOTA 模型接入前，需要先有稳定工程闭环。
- **影响面**：新增离线推理 runtime 与测试；不启用生产 `offline` 配置，不改变在线实时推理链路。

## 改动详情

### 1. `app/services/inference/offline/interfaces.py`

新增离线时序模型运行时契约：

| 类型 | 作用 |
|------|------|
| `OfflineFrame` | 单个时间点的多 source 检测框 |
| `OfflineFeatureSequence` | 完整时序输入，包含任务、步骤、帧序列、source、fps |
| `FramePrediction` | 模型逐帧预测 |
| `TimelineSegment` | 合并后的行为时间段 |
| `OfflineInferenceResult` | 一次离线推理的完整输出 |
| `OfflineTemporalModel` | 后续真实模型统一实现的接口 |

### 2. `app/services/inference/offline/segmenter/`

新增规则 baseline：

- `features.py`：`OfflineFeatureSequence -> [T, 62]` 特征。
- `brush_rule.py`：基于规则输出逐帧动作预测。
- `__init__.py`：模型创建入口。

特征由目标级、目标关系、时间位置三类组成：

```text
9 类目标 * 5 个目标级特征 = 45
7 组目标关系 * 2 个关系特征 = 14
时间位置特征 = 3
总计 = 62
```

当前 baseline 输出：

```text
short_brush_cleaning
long_brush_insert
flush
air_injection
idle
```

`long_brush_withdraw` 暂未强行规则化，后续交给真实时序模型或轨迹特征处理。

### 3. `app/services/inference/offline/segmenters/brush_rule.py`

把规则模型包装成已有 `OfflineSegmenter`：

```text
FeatureStore.load_many(task_id, step_id, sources)
  -> OfflineRunner
  -> BrushRuleSegmenter.preprocess(...)
  -> BrushRuleSegmenter.segment(...)
  -> list[SegmentFact]
  -> FactLedger.replace_segments(...)
```

这条链路和一期图中的 `FeatureStore -> OfflineAnalyzer -> SegmentFact -> FactLedger` 对齐。当前没有单独 `OfflineJudge` 类，timeline 暂以 `SegmentFact` 形式表达。

### 4. `app/services/inference/offline/worker.py`

新增手动 worker：

```powershell
python -m app.services.inference.offline.worker run --task-id <id> --step-id <id> --storage-base-dir database --write-ledger
python -m app.services.inference.offline.worker query --task-id <id> --step-id <id> --storage-base-dir database
```

输入：

```text
{storage_base_dir}/{task_id}/{step_id}/features.jsonl
```

输出：

```text
offline_inference_result.json
facts.jsonl
```

读取 `features.jsonl` 时使用 `utf-8-sig`，兼容 Windows 手工写入 JSONL 时可能出现的 UTF-8 BOM。

### 5. 测试覆盖

新增 `tests/test_offline_inference_worker.py`，覆盖 worker 从 `features.jsonl` 读取检测序列、生成 timeline、写入 `facts.jsonl`、查询时间线，以及 Windows UTF-8 BOM JSONL 兼容。

## 数据通道

| 阶段 | 输入 | 输出 | 说明 |
|------|------|------|------|
| FeatureStore | 在线链路落盘的检测框 | `features.jsonl` | 每行包含 `ts` 与多 source detections |
| worker / runner | `features.jsonl` 或 `FrameDetections` 序列 | `OfflineFeatureSequence` | 将多 source 检测序列整理成模型统一输入 |
| baseline segmenter | `OfflineFeatureSequence` | `FramePrediction` / `TimelineSegment` | 当前为规则模型，后续可替换真实模型 |
| SegmentFact 转换 | timeline | `SegmentFact` | 对齐后端事实账本格式 |
| FactLedger | `SegmentFact` | `facts.jsonl` | 按 producer / model version 幂等替换 |

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | `324 passed in 39.60s`（`CLEANSIGHT_ENV=test` + dummy DB/API 环境变量，未读取本机 `.env.dev`） |
| 离线专项 pytest | `35 passed in 2.18s` |
| Worker 命令行回环 | 成功输出 `short_brush_cleaning: 0.1-0.4`、`air_injection: 0.4-0.6`，并可从 `facts.jsonl` query |
| 开发库只读连通性 | `select 1 -> 1`，未写业务表 |

## 风险与后续

- 规则 baseline 只用于工程闭环，不代表最终模型精度。
- `long_brush_withdraw` 需要方向/轨迹或真实时序模型支持。
- 自动调度、离线 Judge、离线复算告警、结果入库仍未实现。
