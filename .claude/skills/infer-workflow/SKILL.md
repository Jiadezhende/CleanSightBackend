---
name: infer-workflow
description: "Create a new detection workflow for CleanSightBackend. Use this skill whenever the user asks to create a new detection task, add a new workflow, implement a new XX detection, add a Detector/Analyzer/Judge, or follow the workflow pattern. Trigger on: 新建workflow、新建检测任务、按workflow规范、创建XX检测、add a workflow for、implement detection workflow、新建检测器、新建时序分析器、新建Detector、新建Judge."
---

# 检测 Workflow 开发规范

一个检测任务 = **Detector(L1) + TemporalAnalyzer(L3) + Judge(L4)**，由 YAML 装配。

骨架在 [references/templates.md](references/templates.md)，字段在 [references/data-models.md](references/data-models.md)，装配在 [references/yaml-config.md](references/yaml-config.md)。**架构原理（为什么这样分层）见 [workflows/claude.md](../../../app/services/inference/workflows/claude.md)，本 skill 只讲怎么做。**

## 三层契约

| 层 | 基类 | 实例 | 输入 → 输出 | 必实现 |
|----|------|------|-----------|--------|
| L1 Detector | `Detector` / `YOLODetector` | 多 Client 共享 | 帧 → `DetectionOutput` | `prepare_visualization_data`；YOLO 优先 override `infer_batch` |
| L3 TemporalAnalyzer | `TemporalAnalyzer` | 每 Client 一个 | `List[DetectionOutput]` 滑窗 → `List[EventFact]` | `trans` / `infer` / `post_process`（**别 override `run`**） |
| L4 Judge | `Judge` | 每 Client 一个 | `List[EventFact]` → `(events, alarms)` | `step`；可选 `finalize` |

- **Analyzer 只量事实（产 EventFact），Judge 才下判断（出告警）**。阈值/required 放 Judge，不进 `EventFact.meta`。
- **绑定**：`Detector.name == Analyzer.name == Judge.name`，三者必须一致（系统据此用 `get_slide_window(name)` 串联）。运行时 `analyzer.run(window) → judge.step(facts)` 同步两步走。

## 两种告警模式（都在 Judge）

| | 实时 (REALTIME) | 结算 (SETTLEMENT) |
|---|---|---|
| 触发 | `Judge.step()`，1Hz 上升沿 | `Judge.finalize()`，terminate 时一次 |
| 防重 | 锁存 `self._sm["alarming"]`（0→1 发，1→0 复位） | 无需锁存 |
| YAML | 默认 | 加 `realtime: false` |

实时告警边沿触发模板（在 `step()` 内）：
```python
if is_triggered and not self._sm["alarming"]:
    self._sm["alarming"] = True
    alarms.append(AlarmInfo(...))
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
⚠️ **指标窗口自管**：在 `self._sm` 维护定长 history 按 `self.window_seconds` 裁剪（见 [bubble.py](../../../app/services/inference/workflows/bubble.py) 的 `new_count_history`），**别**把 `get_slide_window` 返回长度当指标窗口。

## Analyzer.infer() 两条路径

- **纯逻辑状态机**（默认，bubble/bending/mock）：推进 `self._sm` 算指标。
- **内嵌轻量时序模型**（按需）：`__init__` 里 `torch.jit.load(path).eval()`，`infer()` 用 `torch.no_grad()` 前向。规则：每实例自加载（**不建 registry/基类、不转 onnx**）；⚠️ **滑窗长度必须 ≥ 模型感受域**，不足则不前向。重模型全序列分割（MS-TCN 类）属离线链路，不在此。

## 选模板（→ [templates.md](references/templates.md)）

| 场景 | 模板 | 参考 |
|------|------|------|
| YOLO + 实时告警（最常见） | A | [bubble.py](../../../app/services/inference/workflows/bubble.py) |
| 无模型 / 纯算法 | B | [mock.py](../../../app/services/inference/workflows/mock.py) |
| 结算式告警 | C | [bending.py](../../../app/services/inference/workflows/bending.py) |
| 在线轻量序列模型（analyzer 内嵌） | D | infer() 路径② |

## 必查清单（⚠️ = 高频 bug）

**Detector**
- [ ] 选基类：YOLO → `YOLODetector`；无模型 → `Detector`
- [ ] `name` 写死、三层一致；实现 `prepare_visualization_data`
- [ ] 优先 override `infer_batch`（try 批量 + except 逐帧 fallback）
- [ ] ⚠️ batch 与 fallback 的业务字段赋值逻辑一致
- [ ] ⚠️ `class_name` 取自模型 `result.names`，与训练类别名严格一致（不归一化）

**Analyzer（只产 EventFact）**
- [ ] `__init__` 全量初始化 `self._sm`（含游标 `last_ts`）
- [ ] 实现 `trans/infer/post_process`；`infer` 推游标算指标，`post_process` 包成 EventFact，**不判告警**
- [ ] ⚠️ 指标窗口自管；模型型：窗口 ≥ 感受域 + `torch.no_grad()`

**Judge（出告警）**
- [ ] 阈值/required 进构造参数；`self._sm` 存决策态（如 `{"alarming": False}`）
- [ ] `step`：先 `frame = self._frame(facts)` 再按 signal 名取值；实时走上升沿锁存
- [ ] 结算告警 override `finalize`

**装配**（→ [yaml-config.md](references/yaml-config.md)）
- [ ] `inference_config.yaml` 加 `class`/`analyzer_class`/`judge_class` + 对应 `*_params`；纯结算加 `realtime: false`
- [ ] [workflows/\_\_init\_\_.py](../../../app/services/inference/workflows/__init__.py) 补三个类的 import 与 `__all__`

> 接口签名照抄基类；`AlarmInfo` 只有 4 字段（`alarm_mode`/`alarm_metric` 系统自动补）。
