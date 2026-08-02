---
name: infer-workflow
description: "Create a new detection workflow for CleanSightBackend. Use this skill whenever the user asks to create a new detection task, add a new workflow, implement a new XX detection, add a Detector/Operator (流源/流算子), or follow the workflow pattern. Trigger on: 新建workflow、新建检测任务、按workflow规范、创建XX检测、add a workflow for、implement detection workflow、新建检测器、新建流算子、新建Detector、新建Operator、新建时序算子."
---

# 检测 Workflow 开发规范

一个检测任务 = **流源 Detector + 流算子 Operator**，由 YAML 的 `detectors[]` + `rules[]` 装配。

**落点（一文件一基类，同业务同名文件）**：Detector 写 `detection/impl/<业务>.py`，Operator 写 `temporal/impl/<业务>.py`，可选离线 Segmenter 写 `offline/impl/<业务>.py`。三者靠同名文件 + config stage 绑定表达业务聚合（不再有单独的 `workflows/` 目录，也别把 Detector 和 Operator 塞进同一文件）。

骨架在 [references/templates.md](references/templates.md)，字段在 [references/data-models.md](references/data-models.md)，装配在 [references/yaml-config.md](references/yaml-config.md)。**架构原理（为什么两层、流源/流算子怎么分）见 [DESIGN_DETECTION_WORKFLOW.md](../../../docs/kb/DESIGN_DETECTION_WORKFLOW.md) 与 [operator.py](../../../app/services/inference/temporal/operator.py) docstring，本 skill 只讲怎么做。** 内嵌时序模型（GRU/Transformer）的算子接入另见 `/temporal-review`。

## 两层契约

| 层 | 基类 | 实例 | 输入 → 输出 | 必实现 |
|----|------|------|-----------|--------|
| 流源 Detector | `Detector` / `YOLODetector` | 无状态，多 Client 共享 | 帧 → `FrameDetections` | `prepare_visualization_data`；YOLO 优先 override `infer_batch(frames, timestamps)` |
| 流算子 Operator | `Operator`（或 `GRUOperator`） | 每 Client 一个，持 `_sm` | 订阅流滑窗 → `(events, alarms)` | `analyze(windows)` 推 `_sm`、`judge()` 出结果；可选 `finalize()` |

- **合并即真源**：analyze（量事实）+ judge（下判断）共享同一份 `self._sm`，不再有 EventFact 对象间传输、不再有双状态机同步。
- **身份两维正交**：`name` = 算子自身/输出身份（日志/告警归属）；`subscribes` = 输入流清单（**显式必填**，元素 = 上游 `detector.name`）。**算子名 ≠ 流名。**
- **绑定**：`detector.name` = 流名 = `slide_window` key = 某算子 `subscribes` 里的元素。系统据此把流喂给订阅它的算子。

## 多流对齐（Operator 基类工具）

`analyze(windows)` 收到 `{流名: 该流滑窗快照(ts 升序)}`，用基类工具取历史，别自己重造窗：

- 单订阅：`self.primary_window(windows)` → 首个订阅流裁到 `window_seconds` 感受野。
- 多订阅：`self._zip_by_ts(windows)` → 按 ts inner-join 对齐成 `List[AlignedFrame]`（仅保留各流都到齐的 ts；依赖同帧多流 ts 精确相等）。
- `self._clip(window)` → 裁到感受野的底层工具。

## 两种告警模式（都在 Operator）

| | 实时 (realtime) | 结算 (settlement) |
|---|---|---|
| 触发 | `judge()`，1Hz 上升沿 | `finalize()`，terminate 时一次 |
| 防重 | 锁存 `self._sm["alarming"]`（0→1 发，1→0 复位） | 无需锁存 |
| YAML | `realtime: true`（纳入 signals_10s） | `realtime: false` |

实时告警边沿触发模板（在 `judge()` 内）：
```python
if is_triggered and not self._sm["alarming"]:
    self._sm["alarming"] = True
    alarms.append(Alarm(..., metric=AlarmMetric.XXX))   # metric 由算子显式填
elif not is_triggered and self._sm["alarming"]:
    self._sm["alarming"] = False
```

## 游标（必守）

滑窗是非破坏性快照，连续 tick 大量重叠。涉及**跨帧累加/计数/喂追踪器**时必须用游标，否则同一帧被重复处理、指标虚高（纯瞬时判断除外）：
```python
last_ts = self._sm["last_ts"]
new_frames = [f for f in window if f.timestamp > last_ts]
for f in new_frames: ...                       # 累加 / 喂 ByteTrack
if new_frames: self._sm["last_ts"] = new_frames[-1].timestamp
```
⚠️ **指标窗口自管**：`primary_window`/`_zip_by_ts` 已把窗裁到感受野，但派生 history（如出生率 `new_count_history`）仍要在 `self._sm` 里按 `self.window_seconds` 自行裁剪（见 [temporal/impl/bubble.py](../../../app/services/inference/temporal/impl/bubble.py)）。

## Operator.analyze() 两条路径

- **纯逻辑状态机**（默认，bubble/bending/mock）：游标推进 `self._sm` 算指标/计数。
- **内嵌因果序列模型**（`GRUOperator` 子类，见 temporal/impl/clean.py）：基类惰性加载 `GRUClassifier`、给 `infer(features)` 出逐帧类别；子类只写 `_adapt_to_features(aligned)→Tensor` + analyze/judge。规则：**模型必须因果**（单向 GRU/causal mask，需未来帧的 MS-TCN 类走离线链路）；⚠️ **窗口帧数 ≥ 感受域**，不足加 warm-up guard（`min_frames`）不前向。接入细则与上线门禁走 `/temporal-review`。

## 选模板（→ [templates.md](references/templates.md)）

> 参考文件均为「Detector 在 `detection/impl/<业务>.py`、Operator 在 `temporal/impl/<业务>.py`」的同名对。

| 场景 | 模板 | 参考（检测器 / 算子） |
|------|------|------|
| YOLO + 实时告警（最常见） | A | [detection/impl/bubble.py](../../../app/services/inference/detection/impl/bubble.py) / [temporal/impl/bubble.py](../../../app/services/inference/temporal/impl/bubble.py) |
| 无模型 / 纯算法 | B | [detection/impl/mock.py](../../../app/services/inference/detection/impl/mock.py) / [temporal/impl/mock.py](../../../app/services/inference/temporal/impl/mock.py) |
| 结算式告警 | C | [detection/impl/bending.py](../../../app/services/inference/detection/impl/bending.py) / [temporal/impl/bending.py](../../../app/services/inference/temporal/impl/bending.py) |
| 内嵌因果序列模型（多流 GRU） | D | [detection/impl/clean.py](../../../app/services/inference/detection/impl/clean.py) / [temporal/impl/clean.py](../../../app/services/inference/temporal/impl/clean.py) |

## 必查清单（⚠️ = 高频 bug）

**Detector（流源，无状态）**
- [ ] 选基类：YOLO → `YOLODetector`；无模型 → `Detector`
- [ ] `name` 写死（= 产出流名）；实现 `prepare_visualization_data` 返回 `RenderSpec`
- [ ] YOLO 优先 override `infer_batch(frames, timestamps)`（try 批量 + except 逐帧 fallback）
- [ ] ⚠️ batch 与 fallback 的业务字段赋值逻辑一致；`timestamps[i]` 原样写入 `FrameDetections.timestamp`（**别自造时间戳**，否则多流 `_zip_by_ts` 漏帧）
- [ ] ⚠️ `class_name` 取自模型 `result.names`，与训练类别名严格一致（不归一化）

**Operator（流算子，analyze 推 `_sm` / judge 出告警）**
- [ ] `__init__` 全量初始化 `self._sm`（含游标 `last_ts`）；`subscribes` 显式传入
- [ ] `analyze(windows)` 用 `primary_window`/`_zip_by_ts` 取窗、推游标算指标，**不返回值**
- [ ] `judge()` 读 `_sm` 出 `(events, alarms)`：events 是 overlay 字符串、alarms 是 `Alarm`
- [ ] ⚠️ 指标窗口自管；模型型：窗口 ≥ 感受域 + warm-up guard（细则走 `/temporal-review`）
- [ ] 实时走上升沿锁存 `_sm["alarming"]`；纯结算 override `finalize()`

**装配**（→ [yaml-config.md](references/yaml-config.md)）
- [ ] `inference_config.yaml` 对应 stage：`detectors[]` 加 detector（`name`/`class`/`params`），`rules[]` 加 operator（`name`/`subscribes`/`realtime`/`class`/`params`）
- [ ] 新告警指标 → 在 [AlarmMetric](../../../app/domain/alarm.py) 枚举补一项，`judge()` 里 `metric=` 显式填
- [ ] **无需**改任何 `impl/__init__.py`（detection/temporal/offline 各一个纯包标记）——StageFactory 按 `class` 全路径 importlib 实例化

> 接口签名照抄基类；`Alarm` 核心 5 字段（`alarm_type`/`alarm_level`/`alarm_message`/`metric`/`metadata`），`mode`/`stage`/`seq`/`timestamp` 落库时自动补。
