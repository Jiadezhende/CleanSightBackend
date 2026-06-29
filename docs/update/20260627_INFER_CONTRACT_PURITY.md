# 推理数据契约提纯：删死字段 + 告警 metric 显式化 + 合并重复落库

> **变更状态**：生效中（2026-06-27）　<!-- 三处清理均已落地，全量 pytest 206 passed -->
> **知识库**：待沉淀
>
> 相关：[20260627_INFRA_ASSEMBLY_DECOUPLE.md](20260627_INFRA_ASSEMBLY_DECOUPLE.md)（同支前一批：装配层解耦）、[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（分层数据流核实）。

## 概述

- **改了什么**：清理 inference **数据契约层**三处裂缝——(P1) 删 `DetectionOutput` 上 4 个领域死字段；(P2) 删 `infer_alarm_metric` 文本反推函数，改由 Judge 显式携带 metric；(P3) 合并两处逐行重复的告警落库映射为单一 `alarm_sink.persist_alarms`。
- **为什么改**：抽象审查发现脊柱（L1/L3/L4 三件套）良好，但数据契约被领域细节污染、且存在"文案↔路由"隐藏耦合。延续上一批"功能 id 全程用、可读名只在出口查一次、杀掉派生重建"的方向（上一批对 *stage*，本批对 *metric*）。
- **影响面**：`data_models.py`、`models.py`、`workflows/{bubble,bending,judge}.py`、新增 `workflows/alarm_sink.py`、`workers/temporal.py`、`core/manager.py`、`CLAUDE.md`。运行时行为不变（死字段无人读、mock metric 前后同为 UNKNOWN）。

| 编号 | 问题 | 风险 |
|------|------|------|
| P1 | `DetectionOutput` 焊死 `bubble_detected/bubble_count/bending_detected/detection_count`（grep 证实全仓 write-only，无读取） | 污染统一契约；逼出两个纯冗余 `infer_batch` override + 一条 skill 同步警告 |
| P2 | `infer_alarm_metric` 用中文文案子串匹配反推 metric（Judge 本就知道）；其 TASK_TIMEOUT 分支为死路径 | 文案与路由耦合，告警文案不能改 |
| P3 | `_persist_alarms` / `_persist_settlement_alarms` 近乎逐行重复 | 改一处忘另一处 |

## 改动详情

### 1. `data_models.py` — `DetectionOutput` 删 4 死字段；`AlarmInfo` 加一等 `metric`

- `DetectionOutput` 删除 `bubble_detected/bubble_count/bending_detected/detection_count`，回归 `detections/metadata/timestamp/success/error` 五字段。派生量统计归 L3 Analyzer，单对象附加量放 `Detection.extra`。
- `AlarmInfo` 新增 `metric: AlarmMetric = AlarmMetric.UNKNOWN`，由产出方（Judge）显式填，下游直接读。

### 2. `workflows/bubble.py` / `workflows/bending.py` — 删 `infer_batch` override

两个 `BubbleDetector` / `BendingDetector` 的 `infer_batch` 仅为写死字段而存在，删除后由基类 [`YOLODetector.infer_batch`](../../app/services/inference/workflows/detector.py) 接管；顺带清理失用的 `time` / `numpy` / `typing` import。Judge 构造 `AlarmInfo` 时填 `metric=AlarmMetric.BUBBLE`（bubble）/ `AlarmMetric.BENDING`（bending，并移除 metadata 里的 `"metric": self.name`）。

### 3. `models.py` — 删除 `infer_alarm_metric`

#### 旧
```python
metric = infer_alarm_metric(alarm_type=..., alarm_message=..., metadata=...)
# 内部：metadata["metric"] 优先，否则 "气泡"/"bend" 中文子串匹配，再否则 TASK_TIMEOUT
```
#### 新
```python
metric = alarm.metric   # 产出方（Judge）已显式填
```
整个函数及其 `AlarmType` import 删除。TASK_TIMEOUT 分支本就死（[monitor.py](../../app/services/health_monitor/monitor.py) 超时仅运维治理、不产业务告警）。

### 4. 新增 `workflows/alarm_sink.py` — 实时/结算共用落库映射（P3）

`persist_alarms(alarms, *, cq, client_id, stage_name, mode, persistence_manager, log_each=False)`：闸门 → `persist_alarm` → `append_alarm_record`，metric 直读 `alarm.metric`。两调用点差异（mode、stage 来源、client_id、是否逐条日志）全部参数化。

- [`workers/temporal.py`](../../app/services/inference/workers/temporal.py) `_persist_alarms`：实时路径，传 `mode=REALTIME`、`stage_name=get_stage_alias(self._stage)`。
- [`core/manager.py`](../../app/services/inference/core/manager.py) `_persist_settlement_alarms`：结算路径，传 `mode="SETTLEMENT"`、`log_each=True`。

### 5. `config.py` — 删整段过时默认配置，改 fail-fast（P1 的同源延伸）

审视发现 `_create_default_config()` 的 schema 已**整体过时**，与当前真源 `config/inference_config.yaml` 全面对不上：

| 维度 | 真实 yaml | 旧默认配置 |
|------|----------|-----------|
| stage 主键 | `"1"`/`"2"` + `alias` | `"LEAK"`/`"CLEAN"`，无 alias（→ 恒等路由全落 MOCK） |
| 类路径 | `inference.workflows.bubble.BubbleDetector` | `app.services.models.bubble.BubbleDetectionTask`（**类不存在**） |
| 时序/规则 | per-model `analyzer_class`/`judge_class` | per-stage `temporal_analyzer`/`visualizer`/`alarm_triggers`（旧 schema，无人读） |

它若被触发只会崩在 StageFactory 或静默错路由——"兜底"比没有更糟。处理：

- 删除 `_create_default_config()`；`load_stage_config` 三处 fallback 改 **fail-fast**（缺文件 `FileNotFoundError`、格式错/解析异常 `ValueError`/上抛）。yaml 是必备单一真源，与 [`InferenceManager._get_stage_configs`](../../app/services/inference/core/manager.py) 既有 fatal 行为一致。
- 删 `StageConfig` 的过时字段 `temporal_analyzer`/`visualizer`/`alarm_triggers`（仅旧 schema 用，无人读）。
- 删死函数 `instantiate_from_config`（无调用方；StageFactory 自带 `_instantiate_from_config`）。

### 6. 保留项（不改动）

- `AlarmMetric.TASK_TIMEOUT` 枚举值保留（可能他处展示用），仅删 `infer_alarm_metric` 内的死分支。

## 数据通道 / 行为说明

| 值 | 写/拥有 | 读 | 本次影响 |
|------|--------|----|---------|
| `AlarmInfo.metric` | Judge（产出方显式填） | `alarm_sink.persist_alarms` → 闸门 + 落库 + AlarmRecord | 来源从"下游文本反推"收敛为"上游显式"，运行时取值不变（bubble→BUBBLE / bending→BENDING / mock→UNKNOWN） |

## 验证

| 项 | 结果 |
|----|------|
| 死字段静态归零 `grep -E 'bubble_detected\|...' app/` | ✅ 仅剩 `models/task.py`(无关 Task 模型) + `config.py` 死 fallback + docstring |
| `infer_alarm_metric` 残留 grep | ✅ app/、tests/ 归零 |
| 关键模块导入冒烟（alarm_sink/temporal/manager/bubble/bending/mock） | ✅ `AlarmInfo.metric` 默认 UNKNOWN |
| 真实 yaml 加载（删默认配置后） | ✅ `list_stages()==['1','2','MOCK']`，alias/models 正确 |
| 缺失 yaml fail-fast | ✅ 抛 `FileNotFoundError`（不再静默兜底） |
| 全量 `pytest tests/` | **206 passed**（与改动前基线一致） |
