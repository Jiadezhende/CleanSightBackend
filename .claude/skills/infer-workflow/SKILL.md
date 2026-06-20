---
name: infer-workflow
description: "Create a new detection workflow for CleanSightBackend. Use this skill whenever the user asks to create a new detection task, add a new workflow, implement a new XX detection, add a Detector/Analyzer/Judge, or follow the workflow pattern. Trigger on: 新建workflow、新建检测任务、按workflow规范、创建XX检测、add a workflow for、implement detection workflow、新建检测器、新建时序分析器、新建Detector、新建Analyzer、新建Judge."
---

# 检测 Workflow 开发规范

CleanSightBackend 每个检测任务 = **Detector（L1 无状态推理）** + **TemporalAnalyzer（L3 有状态时序分析，只产事实）** + **Judge（L4 规则判定，出告警）** 三个组件，由 YAML 装配。本文讲清"为什么这样分层"和"哪里最容易写错"；**代码骨架另在 [references/templates.md](references/templates.md)，字段速查在 [references/data-models.md](references/data-models.md)，装配在 [references/yaml-config.md](references/yaml-config.md)**——写代码时按需翻阅，不要凭记忆抄字段。

> 权威架构参照：[workflows/CLAUDE.md](../../../app/services/inference/workflows/CLAUDE.md)。本 skill 与其保持一致。

---

## 心智模型：三层，职责切分而非调度切分

一个检测任务拆成三层，跑在两个线程（L1 在推理线程 ~30fps，L3+L4 在时序线程 1Hz）：

| 维度 | **L1 Detector** | **L3 TemporalAnalyzer** | **L4 Judge** |
|------|----------------|------------------------|-------------|
| 职责 | 单帧/批量推理 + 准备可视化数据 | 滑窗时序分析，**只产事实** | **消费事实出告警** |
| 状态 | **无状态**（除惰性加载模型权重） | **有状态** `self._sm`（计数器/追踪器/游标） | **有状态** `self._sm`（上升沿锁存/结算累计） |
| 实例 | 多 Client **共享单实例** | **每 Client 独立实例** | **每 Client 独立实例** |
| 输入 | 单帧 `np.ndarray` 或一批帧 | 滑窗快照 `List[DetectionOutput]`（时间升序） | `List[EventFact]`（本 tick） |
| 输出 | `DetectionOutput` | `List[EventFact]` | `(events, alarms)` |
| 基类 | `Detector` / `YOLODetector` | `TemporalAnalyzer` | `Judge` |
| 必实现 | `prepare_visualization_data`；YOLO 任务**优先 override `infer_batch`** | `trans` / `infer` / `post_process`（`run` 由基类提供） | `step`；可选 `finalize` |

**分层是职责切分，不是调度切分**：运行时 `ClientTemporalActor._tick()` 里 `analyzer.run(window) → judge.step(facts)` 是**同步两步走**，靠函数返回值串联，无队列。analyzer 产 `EventFact`，judge 立刻消费。

> **绑定规则（最关键）**：`Detector.name` == `TemporalAnalyzer.name` == `Judge.name`，三者必须一致。系统据此用 `cq.get_slide_window(name)` 把检测结果喂给分析器、把分析器的事实配给 judge。name 不一致 → 分析器永远拿空窗口。

---

## 数据流：EventFact / events / alarms 各去哪

```
帧 ─[Detector.infer_batch]→ DetectionOutput → slide_window[name]
                                                   │ [1Hz] ClientTemporalActor._tick
                              TemporalAnalyzer.run(window)
                                  trans → infer → post_process
                                                   │
                                          List[EventFact]   （只产事实，不判告警）
                                                   │
                                       Judge.step(facts)
                                ┌─────────────────┴─────────────────┐
                             events (List[str])              alarms (List[AlarmInfo])
                       set_latest_temporal → overlay        persist_alarm（30s 批次）
                       → WebSocket 推前端                    → HTTP POST 外部库
```

- **EventFact** = `Analyzer` 产的瞬时事实（某信号在 ts 的当前电平，如 `birth_rate=0.7`），**仅进程内值传递给 Judge，不落盘**。
- **events** = `List[str]`，临时展示信号，渲染成视频 overlay 推前端，**不入库**。由 Judge 产出。
- **alarms** = `List[AlarmInfo]`，正式告警，攒批持久化，**不直接推前端**。由 Judge 产出。

> 记忆点：**Analyzer 量事实，Judge 下判断**；**events 给人看（overlay），alarms 给库存（持久化）**。

---

## 两种告警模式：实时 vs 结算（都归 Judge）

| | **实时告警 (REALTIME)** | **结算式告警 (SETTLEMENT)** |
|---|------------------------|----------------------------|
| 触发时机 | 1Hz tick 条件满足，**上升沿**触发 | 任务 **terminate 时调用一次** |
| 来源方法 | `Judge.step()` 返回的 `alarms` | `Judge.finalize()` 返回的列表 |
| 防重复 | **需锁存** `self._sm["alarming"]`（0→1 发，1→0 复位） | **无需锁存**（只评估一次） |
| 评估依据 | 当前 tick 的事实瞬时值 | 整个任务累计指标 |
| 范例 | `BubbleJudge`（birth_rate 超阈值立刻报） | `BendingJudge`（结束时不足次数才报 warning） |

**实时告警 = 边沿触发**（在 `Judge.step()` 内）：

```python
if is_triggered and not self._sm["alarming"]:      # 0→1 上升沿：发告警
    self._sm["alarming"] = True
    alarms.append(AlarmInfo(...))
elif not is_triggered and self._sm["alarming"]:    # 1→0 下降沿：复位锁存
    self._sm["alarming"] = False
```

> **为什么边沿而非电平触发？** 电平触发在持续异常时每个 tick 都重复投递、淹没下游；边沿触发只在状态变化时发一次，符合告警语义。

一个 Judge 可以**只用实时**（finalize 返回空）、**只用结算**（step 的 alarms 恒空，只产 events），或两者并用。结算式用 `finalize()` + YAML `realtime: false`。

> **阈值/required 归 Judge**，不进 `EventFact.meta`。Analyzer 只报"birth_rate 是多少"，"超没超 0.5"是 Judge 的事。

---

## 游标机制：滑动窗口是非破坏性消费

时序线程每秒 tick 一次，每次都从 `cq.get_slide_window(name)` 拿到**整个窗口的快照副本**（[queues.py](../../../app/services/client/queues.py) 的 `get_slide_window` 返回 `list(window)`）。两个事实决定你必须用游标：

- **消费不清空窗口**：窗口只按时间淘汰，不因被读取而移除。
- 因此**连续两个 tick 的快照大量重叠**——同一帧反复出现在多个 tick 的 window 里。

每个 tick 都对整个 window 重新累加/计数 → **同一帧被处理 N 次** → 计数翻倍、指标虚高。

**解法：游标 `self._sm["last_ts"]`**（在 Analyzer 的 `infer`/`_advance` 里），只处理 timestamp 大于游标的新帧，处理完推进游标：

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

## Analyzer 的 `infer()` 两条路径

`TemporalAnalyzer.run()` 串 `trans → infer → post_process`。`infer()` 是"前向"那一步，有两条实现路径：

### 路径 ①：纯逻辑状态机（现状，最常见）

bubble/bending/mock 都走这条——`infer()` 里推进 `self._sm`（计数、去抖、ByteTrack），返回测得的指标。无任何模型。绝大多数检测任务用这条。

### 路径 ②：内嵌轻量因果时序模型（按需）

当纯逻辑算不出指标、需要一个小的因果序列模型（轻量 TCN/GRU 等）时，**直接在 analyzer 里加载并调用，不要建任何额外基础设施**：

```python
import torch

class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, model_path, receptive_field, name="xxx"):
        super().__init__(name=name)
        m = torch.jit.load(model_path, map_location="cpu"); m.eval()
        self._model = m                          # ~5ms 加载，set_task 路径无感
        self.receptive_field = receptive_field
        self._sm = {"last_ts": 0.0, ...}         # per-client 解码状态

    def trans(self, window):                     # DetectionOutput 列表 → (T, C) ndarray
        ...
    def infer(self, feats):
        if len(feats) < self.receptive_field:    # ⚠️ 窗口必须 ≥ 模型感受域
            return None                          # 不足则不前向（post_process 返空）
        with torch.no_grad():
            return self._model(torch.from_numpy(feats)[None]).numpy()[0]
    def post_process(self, logits, ts):          # logits → List[EventFact]
        ...
```

**为什么不建 registry / 基类 / 不转 onnx**（实测 + 场景意识，当前 2-3 客户端 / 1Hz）：
- `torch.jit.load` 实测 **~5ms**（21MB 模型也才 6ms），共享权重省下的开销趋近于零，纯属过度设计。
- torch 本就是依赖（ultralytics 带），转 onnx 只多一步 export、还踩坑，无收益。
- 每 client 各加载一份、互不共享，**连双重检查锁都不需要**。
- ⚠️ **滑窗长度必须 ≥ 模型感受域**，否则模型看不到足够历史、恒输出无意义结果。
- 等客户端涨到几十、或模型变重（几百 MB）再考虑共享设施——**那是升级触发条件，不是起点**。

> 重模型全序列分割（MS-TCN 类，感受域 ≈2047 帧 ≫ 在线窗口）属**离线链路**，不在本 skill 在线范围。

---

## 选模板

照场景定位，再去 [references/templates.md](references/templates.md) 抄对应骨架（每个骨架都含 Detector + Analyzer + Judge 三件套）：

| 场景 | 模板 | 参考实现 |
|------|------|---------|
| YOLO 模型 + 实时告警（最常见） | **A** | [bubble.py](../../../app/services/inference/workflows/bubble.py) |
| 无模型 / 纯算法（numpy、时间戳…） | **B** | [mock.py](../../../app/services/inference/workflows/mock.py) |
| 实时只显示、任务结束才裁决告警 | **C** | [bending.py](../../../app/services/inference/workflows/bending.py) |
| 在线轻量因果序列模型（analyzer 内嵌时序模型） | **D** | 模板 D 设计要点 + 上方 `infer()` 路径② |

告警模式（实时/结算）由 **Judge** 决定，与模型形态正交，按需组合。

---

## 必查清单

写完对照检查，⚠️ 项是高频 bug 源。

**L1 Detector 子类**
- [ ] 选对基类：YOLO 模型 → `YOLODetector`；无模型/纯算法 → `Detector`
- [ ] 文件 `app/services/inference/workflows/<name>.py`，`__init__` 写死 `name="..."`（与 Analyzer/Judge 一致）
- [ ] 实现 `prepare_visualization_data()` 返回 `VisualizationData`
- [ ] **优先 override `infer_batch()`**：try 批量路径 + except fallback 逐帧 `infer`
- [ ] ⚠️ **batch 路径与 fallback 单帧路径的业务字段赋值逻辑必须一致**
- [ ] ⚠️ `class_name` 直接取自模型 `result.names`，匹配字符串与训练类别名**严格一致**（不归一化）

**L3 TemporalAnalyzer 子类（只产 EventFact）**
- [ ] 与 Detector 同文件；`super().__init__(name=...)` 且 `name` 三层一致
- [ ] `__init__` 完整初始化 `self._sm = {...}`（所有测量状态字段，含游标 `last_ts`）
- [ ] 实现 `trans` / `infer` / `post_process`；**不要 override `run`**（基类已串好）
- [ ] `infer()`：游标推进（跳过 `ts <= last_ts`）→ 更新 `self._sm` → 返回测得指标（纯逻辑）或模型前向（路径②）
- [ ] `post_process()`：把指标包成 `List[EventFact]`（`source`/`signal`/`value`/`ts`），**不在此判告警**
- [ ] ⚠️ **指标自管窗口**：在 `self._sm` 维护定长 history 按 `self.window_seconds` 裁剪，**不要**把 `get_slide_window` 返回长度当指标窗口
- [ ] 模型型 analyzer：⚠️ 窗口 ≥ 感受域；`torch.no_grad()`；不建 registry/基类

**L4 Judge 子类（消费事实出告警）**
- [ ] 与 Analyzer 同文件；`super().__init__(name=...)` 且 `name` 三层一致
- [ ] `__init__` 写死阈值/required（构造参数）+ 决策状态 `self._sm`（如 `{"alarming": False}`）
- [ ] `step(facts)`：先 `frame = self._frame(facts)` 建 `{signal: fact}` 快照，再按信号名取值
- [ ] 实时告警走**上升沿锁存**（0→1 发、1→0 复位）；返回 `(events, alarms)`
- [ ] 结算告警 override `finalize()`（任务结束评估一次，无需锁存）

**装配与注册**（见 [references/yaml-config.md](references/yaml-config.md)）
- [ ] `config/inference_config.yaml` 对应 stage 的 `models` 下加一项：`name` / `class` / `analyzer_class` / `judge_class` / `params` / `analyzer_params` / `judge_params`
- [ ] 纯结算型任务加 `realtime: false`
- [ ] 在 [workflows/\_\_init\_\_.py](../../../app/services/inference/workflows/__init__.py) 补 import 和 `__all__`（Detector + Analyzer + Judge 三个类都要）

> 接口签名照抄基类不要改；告警字段只有 4 个（`alarm_mode`/`alarm_metric` 系统自动补），详见 [references/data-models.md](references/data-models.md)。
