---
name: infer-workflow
description: "Create a new detection workflow for CleanSightBackend. Use this skill whenever the user asks to create a new detection task, add a new workflow, implement a new XX detection, add a Detector/Analyzer, or follow the workflow pattern. Trigger on: 新建workflow、新建检测任务、按workflow规范、创建XX检测、add a workflow for、implement detection workflow、新建检测器、新建时序分析器、新建Detector、新建Analyzer."
---

# 检测 Workflow 开发规范

CleanSightBackend 每个检测任务 = **Detector（无状态推理）** + **TemporalAnalyzer（有状态时序分析）** 两个组件，由 YAML 装配。本文讲清"为什么这样设计"和"哪里最容易写错"；**代码骨架另在 [references/templates.md](references/templates.md)，字段速查在 [references/data-models.md](references/data-models.md)，装配在 [references/yaml-config.md](references/yaml-config.md)**——写代码时按需翻阅，不要凭记忆抄字段。

---

## 心智模型：两个组件，不同状态语义

一个检测任务被拆成两半，跑在不同线程：

| 维度 | **Detector**（检测器） | **TemporalAnalyzer**（时序分析器） |
|------|----------------------|-----------------------------------|
| 职责 | 单帧/批量 GPU 推理 + 准备可视化数据 | 滑动窗口时序分析 + 产出事件与告警 |
| 状态 | **无状态**（除惰性加载的模型权重） | **有状态**（`self._sm`：计数器、追踪器、告警锁存） |
| 实例 | 多 Client **共享单实例** | **每 Client 独立实例**（`set_task()` 时新建） |
| 输入 | 单帧 `np.ndarray` 或一批帧 | 滑动窗口快照 `List[DetectionOutput]`（时间升序） |
| 输出 | `DetectionOutput` | `(events, alarms)` 元组 |
| 基类 | `Detector` / `YOLODetector`（[detector.py](../../../app/services/inference/workflows/detector.py)） | `TemporalAnalyzer`（[analyzer.py](../../../app/services/inference/workflows/analyzer.py)） |
| 必实现 | `prepare_visualization_data`；YOLO 任务**优先 override `infer_batch`** | `analyze_temporal`；可选 `finalize` |

**为什么 Detector 优先实现 `infer_batch`**：生产热路径 `MultiModelWorkerPool` 调用 `model.infer_batch(frames, contexts)` —— 组批一次喂给 YOLO 显著提升 GPU 吞吐；单帧 `infer` 只在 batch 失败时作 fallback。`YOLODetector` 已封装模型加载与默认批量推理，子类 override 通常只为补业务字段。

> **绑定规则（最关键）**：`Detector.name` **必须等于** 配对 `TemporalAnalyzer.name`。系统据此用 `cq.get_slide_window(name)` 把检测结果喂给对应分析器。两个 name 不一致 → 分析器永远拿到空窗口。

---

## 数据流：events 和 alarms 去哪

```
帧 ─[Detector.infer_batch]→ DetectionOutput → slide_window[name]
                                                   │ [1Hz] ClientTemporalActor._tick
                                  TemporalAnalyzer.analyze_temporal(window)
                                                   │
                                ┌─────────────────┴─────────────────┐
                             events (List[str])              alarms (List[AlarmInfo])
                       set_latest_temporal → overlay        persist_alarm（30s 批次）
                       → WebSocket 推前端                    → HTTP POST 外部库
```

- **events** = `List[str]`，临时展示信号，渲染成视频 overlay 推前端，**不入库**。
- **alarms** = `List[AlarmInfo]`，正式告警，攒批持久化，**不直接推前端**。

> 记忆点：**events 给人看（overlay），alarms 给库存（持久化）**，互不替代。

---

## 两种告警模式：实时 vs 结算

| | **实时告警 (REALTIME)** | **结算式告警 (SETTLEMENT)** |
|---|------------------------|----------------------------|
| 触发时机 | 1Hz tick 条件满足，**上升沿**触发 | 任务 **terminate 时调用一次** |
| 来源方法 | `analyze_temporal()` 返回的 `alarms` | `finalize()` 返回的列表 |
| 防重复 | **需锁存** `self._sm["alarming"]`（0→1 发，1→0 复位） | **无需锁存**（只评估一次） |
| 评估依据 | 当前滑动窗口瞬时指标 | 整个任务累计指标 |
| 范例 | `BirthRateAnalyzer`（漏气超阈值立刻报） | `DebounceAnalyzer`（结束时不足次数才报 warning） |

**实时告警 = 边沿触发**（rising edge 发一次，falling edge 复位）：

```python
if is_triggered and not self._sm["alarming"]:      # 0→1 上升沿：发告警
    self._sm["alarming"] = True
    alarms.append(AlarmInfo(...))
elif not is_triggered and self._sm["alarming"]:    # 1→0 下降沿：复位锁存
    self._sm["alarming"] = False
```

> **为什么边沿而非电平触发？** 电平触发在持续异常时每个 tick 都重复投递告警、淹没下游；边沿触发只在状态变化时发一次，符合告警语义。

一个分析器可以**只用实时**（finalize 返回空）、**只用结算**（analyze_temporal 的 alarms 恒空），或两者并用。结算式用 `finalize()` + YAML `realtime: false`。

---

## 游标机制：滑动窗口是非破坏性消费

时序线程每秒 tick 一次，每次都从 `cq.get_slide_window(name)` 拿到**整个窗口的快照副本**（[queues.py](../../../app/services/client/queues.py) 的 `get_slide_window` 返回 `list(window)`）。两个事实决定你必须用游标：

- **消费不清空窗口**：窗口只按时间淘汰，不因被读取而移除。
- 因此**连续两个 tick 的快照大量重叠**——同一帧反复出现在多个 tick 的 window 里。

每个 tick 都对整个 window 重新累加/计数 → **同一帧被处理 N 次** → 计数翻倍、指标虚高。

**解法：游标 `self._sm["last_ts"]`**，只处理 timestamp 大于游标的新帧，处理完推进游标：

```python
last_ts = self._sm["last_ts"]
new_frames = [f for f in window if f.timestamp > last_ts]   # 只取新帧
for f in new_frames:
    ...                                                      # 累加 / 喂 ByteTrack / 计数
if new_frames:
    self._sm["last_ts"] = new_frames[-1].timestamp          # 推进游标
```

> **两个"窗口"别混淆**：
> - **缓冲保留窗口**（ClientQueues 全局 `_slide_window_seconds`，决定 `get_slide_window` 返回多长历史，运维可能调大）；
> - **指标窗口**（Analyzer 自己的 `self.window_seconds`，决定在多长时间上算 birth_rate 等指标）。
>
> Analyzer 必须**自管指标窗口**：在 `self._sm` 里维护定长 history 并按 `self.window_seconds` 裁剪（见 [bubble.py](../../../app/services/inference/workflows/bubble.py) 的 `new_count_history`），**绝不能把"返回的 window 长度"直接当指标窗口**。否则全局缓冲一旦调大，触发/锁存窗口被动变长 → 告警冷却过久、第二次真实事件被吞。

> **例外**：纯瞬时判断（"最新一帧是否命中"）可不用游标；但凡涉及**跨帧累加、计数或喂追踪器**，游标必须有。

---

## 选模板

照场景定位，再去 [references/templates.md](references/templates.md) 抄对应骨架：

| 场景 | 模板 | 参考实现 |
|------|------|---------|
| YOLO 模型 + 实时告警（最常见） | **A** | [bubble.py](../../../app/services/inference/workflows/bubble.py) |
| 无模型 / 纯算法（numpy、时间戳…） | **B** | [mock.py](../../../app/services/inference/workflows/mock.py) |
| 实时只显示、任务结束才裁决告警 | **C** | [bending.py](../../../app/services/inference/workflows/bending.py) |
| 长上下文 / 低频序列模型（动作分割等） | **D** | 模板 D 设计要点 |

实时 + 结算可叠加；模型形态与告警模式正交，按需组合。

---

## 必查清单

写完对照检查，⚠️ 项是高频 bug 源。

**Detector 子类**
- [ ] 选对基类：YOLO 模型 → `YOLODetector`；无模型/纯算法 → `Detector`
- [ ] 文件 `app/services/inference/workflows/<name>.py`，`__init__` 写死 `name="..."`（与 Analyzer 一致）
- [ ] 实现 `prepare_visualization_data()` 返回 `VisualizationData`
- [ ] **优先 override `infer_batch()`**：try 批量路径 + except fallback 逐帧 `infer`
- [ ] ⚠️ **batch 路径与 fallback 单帧路径的业务字段赋值逻辑必须一致**
- [ ] ⚠️ `class_name` 直接取自模型 `result.names`，匹配字符串与训练类别名**严格一致**（不归一化）

**TemporalAnalyzer 子类**
- [ ] 与 Detector 同文件；`super().__init__(name=...)` 且 `name` 与 Detector 一致
- [ ] `__init__` 完整初始化 `self._sm = {...}`（所有状态字段，含游标 `last_ts`）
- [ ] `analyze_temporal()`：游标推进（跳过 `ts <= last_ts`）→ 更新 `self._sm` → 算指标 → 产 events/告警
- [ ] ⚠️ **指标自管窗口**：在 `self._sm` 维护定长 history 按 `self.window_seconds` 裁剪，**不要**把 `get_slide_window` 返回长度当指标窗口
- [ ] 实时告警走**上升沿锁存**；结算告警 override `finalize()`

**装配与注册**（见 [references/yaml-config.md](references/yaml-config.md)）
- [ ] `config/inference_config.yaml` 对应 stage 的 `models` 下加一项：`name` / `class` / `analyzer_class` / `params` / `analyzer_params`
- [ ] 纯结算型任务加 `realtime: false`
- [ ] 在 [workflows/\_\_init\_\_.py](../../../app/services/inference/workflows/__init__.py) 补 import 和 `__all__`

> 接口签名照抄基类不要改；告警字段只有 4 个（`alarm_mode`/`alarm_metric` 系统自动补），详见 [references/data-models.md](references/data-models.md)。
