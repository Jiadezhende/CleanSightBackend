# InferenceWorkflow 拆分重构说明

> 重构日期：2026-04-08  
> 涉及分支：`refact/workflow`

---

## 背景

重构前，`InferenceWorkflow` 是一个 God Class，同时承担三件完全不同的事：

| 职责 | 运行线程 | 状态类型 |
|------|---------|---------|
| GPU 批量推理（~30fps） | 推理线程 | 无状态 |
| 时序分析 + 状态机（1Hz） | 时序线程 | per-client 有状态 |
| 可视化数据准备 | 可视化线程 | 无状态 |

三个线程共用同一个对象，导致：

- 状态机（ByteTrack、计数器、告警锁存）被错误地多路 client 共享
- `sm` 状态字典作为外部参数在调用层和实现层之间往返传递，职责边界不清
- `ClientTemporalActor` 从未被正确创建（Bug #2）
- 结算告警调用 `finalize(sm)` 时传入了 `ClientState` 对象而非 `sm` 字典（Bug #1）

---

## 重构目标

将 `InferenceWorkflow` 拆分为两个职责清晰的基类，并为每个基类分配正确的线程和实例化策略：

| 类 | 职责 | 状态 | 实例化 | 运行线程 |
|----|------|------|-------|---------|
| `Detector` | GPU 推理 + 可视化数据准备 | **无状态** | 每个 Stage 一份，所有 Client 共享 | 推理线程 + 可视化线程 |
| `TemporalAnalyzer` | 时序分析 + 状态机 + 告警评估 | **有状态（self._sm）** | `set_task()` 时每个 Client 独立创建 | 时序线程（1Hz） |

---

## 新类层次结构

```
Detector (ABC)                       TemporalAnalyzer (ABC)
├── YOLODetector                     ├── BirthRateAnalyzer  (bubble, 漏气出生率)
│   ├── BubbleDetector               ├── DebounceAnalyzer   (bending, 弯曲去抖)
│   └── BendingDetector              └── MockAnalyzer       (mock, 测试用)
└── MockDetector
```

### `Detector` ABC

```python
class Detector(ABC):
    name: str       # 路由键，必须与配对 TemporalAnalyzer.name 一致
    enabled: bool

    @abstractmethod
    def infer(self, frame, context) -> DetectionOutput: ...

    @abstractmethod
    def prepare_visualization_data(self, output) -> VisualizationData: ...

    def infer_batch(self, frames, contexts) -> List[DetectionOutput]:
        # 默认：逐帧调用 infer()；子类可 override 使用原生 batch 接口
```

`YOLODetector` 在此基础上封装了 YOLO 模型加载（惰性）、`_run_yolo_batch()`、`_adapt_output()` 等样板代码，子类只需实现 `prepare_visualization_data()`。

### `TemporalAnalyzer` ABC

```python
class TemporalAnalyzer(ABC):
    name: str           # 路由键，与配对 Detector.name 一致
    _sm: Dict[str, Any] # 状态机，子类 __init__ 负责完整初始化

    @abstractmethod
    def analyze_temporal(self, window: List[DetectionOutput]) -> Tuple[List[str], List[AlarmInfo]]:
        # window 来自 cq.get_slide_window(self.name)
        # 内部操作 self._sm，不接受外部 sm 参数

    def finalize(self) -> List[AlarmInfo]:
        return []  # 子类按需 override（如弯曲不足时产出结算告警）
```

**关键设计**：`_sm` 是 `TemporalAnalyzer` 的成员变量，完全内化，不在调用链传递。

---

## 数据流

```
RTSP 帧 (30fps)
  ↓
StageAwareDispatcher（轮询，按 stage 分组）
  ↓
MultiModelWorkerPool.infer_batch()
  ├─ BubbleDetector.infer_batch()  → DetectionOutput
  └─ BendingDetector.infer_batch() → DetectionOutput
  ↓
_write_back_results()（双写）
  ├─ cq.push_detection("bubble", output)   → _slide_window["bubble"]
  └─ cq.push_detection("bending", output)  → _slide_window["bending"]
         cq.set_latest_inference(result)   → _latest_inference（可视化用）

ClientTemporalActor._tick()（1Hz，per-client）
  ├─ window = cq.get_slide_window("bubble")
  │    └─ BirthRateAnalyzer.analyze_temporal(window)
  │         → self._sm 内部更新，返回 (events, alarms)
  └─ window = cq.get_slide_window("bending")
       └─ DebounceAnalyzer.analyze_temporal(window)
            → self._sm 内部更新，返回 (events, alarms)
  → cq.set_latest_temporal(all_events)
  → persistence_manager.persist_alarm(alarms)

VisualizationWorkerPool（~15fps）
  └─ cq.get_latest_inference() + cq.get_latest_frame() + cq.get_latest_temporal()
       → Detector.prepare_visualization_data()
       → 渲染 overlay → cq.set_latest_rendered()
```

### 路由机制

`_slide_window` 是 `Dict[task_name, Deque[DetectionOutput]]`，以 `Detector.name` 为键写入，以 `TemporalAnalyzer.name` 为键读取。**两者 name 必须一致**（当前通过硬编码保证：`BubbleDetector.name = BirthRateAnalyzer.name = "bubble"`）。

---

## Actor 生命周期

```
POST /api/start
  └─ InferenceManager.set_task(client_id, task)
       ├─ 停止旧 actor（_stop_event.set()）
       ├─ 从 stage_configs["analyzer_specs"] 实例化 analyzers
       │    analyzers = [BirthRateAnalyzer(...), DebounceAnalyzer(...)]
       └─ ClientTemporalActor(client_id, cq, stage, analyzers).start()

POST /api/terminate
  └─ InferenceManager.remove_client(client_id)
       ├─ actor.finalize_and_stop()          # join 线程，收集结算告警
       ├─ _persist_settlement_alarms()       # 写 DB（不写 _alarm_log）
       ├─ cq.set_latest_temporal([])         # 立即清空前端可见状态
       ├─ cq.set_latest_rendered(None)
       └─ _flush_all_remaining_segments()    # HLS 落盘
  └─ client_manager.remove_client(cleanup=True)
       └─ cq.clear()                         # 清空所有队列
```

**前端状态清零时机**：`actor.finalize_and_stop()` 返回后立即执行（第三步），早于 `cq.clear()`，避免 WebSocket 读到任务结束后的残留 temporal events。

---

## 文件变更清单

### 新建

| 文件 | 内容 |
|------|------|
| `app/services/inference/workflows/detector.py` | `Detector` ABC + `YOLODetector`（YOLO 推理基础设施） |
| `app/services/inference/workflows/analyzer.py` | `TemporalAnalyzer` ABC |

### 重写

| 文件 | 变更要点 |
|------|---------|
| `workflows/bubble.py` | `BubbleDetectionTask` → `BubbleDetector` + `BirthRateAnalyzer`，`_sm` 内化 |
| `workflows/bending.py` | `EndoscopeBendingDetectionTask` → `BendingDetector` + `DebounceAnalyzer`，`_sm` 内化 |
| `workflows/mock.py` | `MockTask` → `MockDetector` + `MockAnalyzer` |
| `workers/temporal.py` | `tasks + _sms` → `analyzers`，调用 `analyzer.analyze_temporal(window)`（无 sm 参数） |

### 删除

| 文件 | 原因 |
|------|------|
| `workflows/infer_workflow.py` | 被 `detector.py` + `analyzer.py` 替代 |
| `client/state.py` | `ClientState._stage` 并入 `ClientQueues.get_stage()` / `set_stage()` |

### 修改

| 文件 | 变更要点 |
|------|---------|
| `workflows/__init__.py` | 导出新类层次，移除旧名称 |
| `client/queues.py` | 内置 `_stage` 字段，添加 `get_stage()` / `set_stage()`；`set_latest_rendered` 接受 `Optional[FrameData]` |
| `client/__init__.py` | 移除 `ClientState` 导出 |
| `core/dispatcher.py` | `cq.state.get_stage()` → `cq.get_stage()` |
| `config/inference_config.yaml` | 每个 model 条目新增 `analyzer_class` + `analyzer_params` |
| `stage_factory.py` | 新增 `create_analyzer_specs_for_stage()` → 返回 `List[Tuple[Type, Dict]]` |
| `core/manager.py` | `_get_stage_configs` 返回 `analyzer_specs`；`set_task` 创建 actor；`remove_client` 用 actor 收集结算告警并提前清空前端状态 |
| `workers/base.py` | 类型注解 `Sequence[InferenceWorkflow]` → `Sequence[Detector]` |
| `inference/__init__.py` | 导出新类，移除旧类（`InferenceWorkflow`、`BubbleDetectionTask`、`EndoscopeBendingDetectionTask`） |
| `workers/__init__.py` | `TemporalWorkerPool` → `ClientTemporalActor` |
| `tests/test_temporal_debounce.py` | 完整重写：移除 `ClientState`，直接实例化 `BirthRateAnalyzer` / `DebounceAnalyzer`，断言 `analyzer._sm` 字段 |

---

## YAML 配置格式

每个 model 条目现在包含四个键：

```yaml
stages:
  LEAK:
    models:
      - name: bubble_detection
        class: app.services.inference.workflows.bubble.BubbleDetector
        analyzer_class: app.services.inference.workflows.bubble.BirthRateAnalyzer
        params:                          # 传给 BubbleDetector.__init__
          model_path: ./app/data/bubble-best.pt
          conf_threshold: 0.5
          iou_threshold: 0.45
          enabled: true
        analyzer_params:                 # 传给 BirthRateAnalyzer.__init__（per-client 实例化）
          birth_rate_threshold: 0.5
          window_seconds: 3.0
```

- `class` + `params`：`StageFactory.create_detectors_for_stage()` 使用，直接实例化 Detector（全局共享）
- `analyzer_class` + `analyzer_params`：`StageFactory.create_analyzer_specs_for_stage()` 使用，返回 `(cls, kwargs)` 元组；`InferenceManager.set_task()` 在每次任务绑定时按 Client 独立实例化

---

## 新增检测器开发指南

### 1. 实现 Detector

```python
# app/services/inference/workflows/my_detection.py

class MyDetector(YOLODetector):
    def __init__(self, model_path, conf_threshold=0.5, enabled=True):
        super().__init__(name="my", model_path=model_path,
                         conf_threshold=conf_threshold, enabled=enabled)

    def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
        # 构造检测框、状态栏文本等
        ...
```

### 2. 实现 TemporalAnalyzer

```python
class MyAnalyzer(TemporalAnalyzer):
    def __init__(self, threshold=0.8):
        super().__init__(name="my")   # name 必须与 MyDetector.name 一致
        self._sm = {
            "alarming": False,
            "count": 0,
        }

    def analyze_temporal(self, window):
        # 操作 self._sm，不接受 sm 参数
        ...
        return events, alarms

    def finalize(self):
        # 可选：任务结束时的结算判断
        return []
```

### 3. 注册到 YAML

```yaml
models:
  - name: my_detection
    class: app.services.inference.workflows.my_detection.MyDetector
    analyzer_class: app.services.inference.workflows.my_detection.MyAnalyzer
    params:
      model_path: ./app/data/my-model.pt
      enabled: true
    analyzer_params:
      threshold: 0.8
```

无需修改任何框架代码，重启服务即生效。

---

## 已修复的 Bug

| Bug | 症状 | 根因 | 修复 |
|-----|------|------|------|
| **#1** | 结算告警从不产出 | `_emit_settlement_alarms()` 传入 `cq.state`（`ClientState` 对象）而非 `sm` 字典，`finalize(sm)` 立即报类型错误 | 重构后由 `actor.finalize_and_stop()` 调用 `analyzer.finalize()`（无外部参数），`manager._persist_settlement_alarms()` 负责写 DB |
| **#2** | 时序分析从不运行 | `ClientTemporalActor` 从未被创建 | `InferenceManager.set_task()` 在任务绑定时创建并启动 actor |

---

## 已知约束

**name 契约无运行时校验**：`Detector.name` 和配对 `TemporalAnalyzer.name` 必须相同，否则 `get_slide_window()` 返回空列表，分析器静默失效。当前依赖子类硬编码保证，如需增加安全性，可在 `InferenceManager._get_stage_configs()` 中加入启动时校验。
