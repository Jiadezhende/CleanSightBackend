# InferenceWorkflow 拆分重构

> 重构日期：2026-04-08 | 分支：`refact/workflow`

## 一句话总结

将 God Class `InferenceWorkflow` 拆为 **`Detector`**（无状态，共享）+ **`TemporalAnalyzer`**（有状态，per-client），解决多客户端状态污染问题。

---

## 1. 为什么要重构

重构前 `InferenceWorkflow` 同时承担三个跨线程的职责：

```
InferenceWorkflow (God Class)
├── infer_batch()             → 推理线程，30fps，无状态
├── analyze_temporal(w, sm)   → 时序线程，1Hz，有状态（sm 外部传入）
└── prepare_visualization()   → 可视化线程，15fps，无状态
```

问题：

- 状态机（ByteTrack、计数器、告警锁存）被多 Client **共享同一实例** → 状态互相污染
- `sm` 字典在调用层和实现层之间反复传递，职责边界不清
- `ClientTemporalActor` 从未被正确创建（死代码）
- `finalize(sm)` 调用时传入了错误类型（`ClientState` 而非 `sm` 字典）

---

## 2. 重构后的架构

### 2.1 两个基类，各司其职

| 基类 | 职责 | 状态 | 实例化策略 | 运行线程 |
| --- | --- | --- | --- | --- |
| **Detector** | GPU 推理 + 可视化 | 无状态 | 每 Stage 一份，所有 Client 共享 | 推理 + 可视化 |
| **TemporalAnalyzer** | 时序分析 + 告警 | `self._sm` | 每 Client 独立实例 | 时序（1Hz） |

### 2.2 类层次

```
Detector (ABC)                       TemporalAnalyzer (ABC)
├── YOLODetector                     ├── BirthRateAnalyzer   (bubble)
│   ├── BubbleDetector               ├── DebounceAnalyzer    (bending)
│   └── BendingDetector              └── MockAnalyzer        (mock)
└── MockDetector
```

### 2.3 核心接口

```python
# Detector — 无状态，线程安全
class Detector(ABC):
    name: str
    def infer(self, frame, context) -> DetectionOutput: ...
    def infer_batch(self, frames, contexts) -> List[DetectionOutput]: ...  # 可 override
    def prepare_visualization_data(self, output) -> VisualizationData: ...

# TemporalAnalyzer — 有状态，per-client
class TemporalAnalyzer(ABC):
    name: str                  # 必须与配对 Detector.name 一致
    _sm: Dict[str, Any]        # 状态机，子类 __init__ 初始化

    def analyze_temporal(self, window) -> Tuple[List[str], List[AlarmInfo]]: ...
    def finalize(self) -> List[AlarmInfo]: ...  # 可选 override
```

**关键设计**：`_sm` 是成员变量，不在调用链传递。`analyze_temporal()` 不接受外部 `sm` 参数。

---

## 3. 数据流

```
RTSP (30fps)
  │
  ▼
StageAwareDispatcher ─── 按 stage 分组 ──→ MultiModelWorkerPool.infer_batch()
                                            ├─ BubbleDetector   → DetectionOutput
                                            └─ BendingDetector  → DetectionOutput
                                                      │
                              ┌────────────────────────┘
                              ▼
                    _write_back_results() 双写
                    ├─ cq.push_detection(name, output)  → slide_window[name]
                    └─ cq.set_latest_inference(result)  → 原子快照
                              │
              ┌───────────────┼───────────────┐
              ▼                               ▼
  ClientTemporalActor (1Hz)       VisualizationWorker (~15fps)
  per-client 独立线程              全局共享
  │                               │
  ├─ get_slide_window("bubble")   ├─ get_latest_inference()
  │  └─ BirthRateAnalyzer         │  └─ Detector.prepare_visualization_data()
  ├─ get_slide_window("bending")  ├─ get_latest_temporal()
  │  └─ DebounceAnalyzer          └─ 渲染 overlay → set_latest_rendered()
  │
  ├─ set_latest_temporal(events)     → 前端 overlay
  └─ persist_alarm(alarms)           → 数据库
```

**路由机制**：`slide_window` 以 `Detector.name` 为键写入，以 `TemporalAnalyzer.name` 为键读取。两者 name 必须一致。

---

## 4. Actor 生命周期

### 任务启动

```
POST /api/start
  └─ InferenceManager.set_task(client_id, task)
       ├─ old_actor.finalize_and_stop()          # 等待旧线程退出 + 收集结算告警
       ├─ _persist_settlement_alarms()           # 持久化旧任务的结算告警
       ├─ 实例化 analyzers（per-client）
       │    [BirthRateAnalyzer(...), DebounceAnalyzer(...)]
       └─ ClientTemporalActor(...).start()       # 启动新线程
```

### 任务终止

```
POST /api/terminate
  └─ InferenceManager.remove_client(client_id)
       ├─ actor.finalize_and_stop()              # join 线程 + 收集结算告警
       ├─ _persist_settlement_alarms()           # 写 DB
       ├─ cq.set_latest_temporal([])             # 立即清空前端状态
       ├─ cq.set_latest_rendered(None)
       └─ _flush_all_remaining_segments()        # HLS 落盘
  └─ client_manager.remove_client(cleanup=True)
       └─ cq.clear()
```

### 线程容错

所有 worker 线程（包括 `ClientTemporalActor`）均通过 `guarded_run()` 包装：

- 主循环崩溃时自动重启（最多 3 次，间隔 2s）
- 超过最大次数记录 CRITICAL 日志后停止
- 与 `GuardedExecutor`（函数级重试）互补

---

## 5. YAML 配置

每个检测任务声明 Detector + TemporalAnalyzer 各自的类和参数：

```yaml
stages:
  LEAK:
    models:
      - name: bubble_detection
        class: app.services.inference.workflows.bubble.BubbleDetector
        analyzer_class: app.services.inference.workflows.bubble.BirthRateAnalyzer
        params:                          # → BubbleDetector.__init__
          model_path: ./app/data/bubble-best.pt
          conf_threshold: 0.5
        analyzer_params:                 # → BirthRateAnalyzer.__init__（per-client）
          birth_rate_threshold: 0.5
          window_seconds: 3.0
```

- `class` + `params`：`create_detectors_for_stage()` → 全局共享实例
- `analyzer_class` + `analyzer_params`：`create_analyzer_specs_for_stage()` → 返回 `(cls, kwargs)` 元组，`set_task()` 时 per-client 实例化

---

## 6. 新增检测器指南

只需 3 步，无需修改任何框架代码：

### Step 1 — Detector

```python
# app/services/inference/workflows/my_detection.py
class MyDetector(YOLODetector):
    def __init__(self, model_path, conf_threshold=0.5, enabled=True):
        super().__init__(name="my", model_path=model_path,
                         conf_threshold=conf_threshold, enabled=enabled)

    def prepare_visualization_data(self, output):
        # 构造检测框、状态栏文本等
        ...
```

### Step 2 — TemporalAnalyzer

```python
class MyAnalyzer(TemporalAnalyzer):
    def __init__(self, threshold=0.8):
        super().__init__(name="my")   # name 必须与 Detector 一致！
        self._sm = {"alarming": False, "count": 0}

    def analyze_temporal(self, window):
        # 操作 self._sm，返回 (events, alarms)
        ...

    def finalize(self):
        # 可选：任务结束时的结算判断
        return []
```

### Step 3 — 注册 YAML

```yaml
- name: my_detection
  class: app.services.inference.workflows.my_detection.MyDetector
  analyzer_class: app.services.inference.workflows.my_detection.MyAnalyzer
  params: { model_path: ./app/data/my-model.pt }
  analyzer_params: { threshold: 0.8 }
```

重启服务即生效。

---

## 7. 文件变更清单

### 新建

| 文件 | 内容 |
|------|------|
| `workflows/detector.py` | `Detector` ABC + `YOLODetector` |
| `workflows/analyzer.py` | `TemporalAnalyzer` ABC |
| `utils/worker_guard.py` | `guarded_run()` 线程级自愈包装器 |

### 删除

| 文件 | 原因 |
|------|------|
| `workflows/infer_workflow.py` | 被 `detector.py` + `analyzer.py` 替代 |
| `client/state.py` | `_stage` 并入 `ClientQueues.get_stage()` |

### 重写

| 文件 | 变更 |
|------|------|
| `workflows/bubble.py` | → `BubbleDetector` + `BirthRateAnalyzer` |
| `workflows/bending.py` | → `BendingDetector` + `DebounceAnalyzer` |
| `workflows/mock.py` | → `MockDetector` + `MockAnalyzer` |
| `workers/temporal.py` | → `ClientTemporalActor`（per-client actor） |
| `tests/test_temporal_debounce.py` | 直接测试 Analyzer 实例 |

### 修改

| 文件 | 变更 |
|------|------|
| `core/manager.py` | `set_task()` 创建 actor + 收集结算告警；`stop()` join 所有 actor |
| `stage_factory.py` | 新增 `create_analyzer_specs_for_stage()` |
| `core/dispatcher.py` | `cq.state.get_stage()` → `cq.get_stage()`；`print()` → `logger` |
| `client/queues.py` | 内置 `_stage`；`set_latest_rendered` 接受 `Optional` |
| `config/inference_config.yaml` | 每条 model 新增 `analyzer_class` + `analyzer_params` |
| `health_monitor/monitor.py` | 孤儿 decoder 清理不再依赖 `has_stream()` |
| `stream/decoder.py` | stderr 线程异常处理（`ValueError` 静默 + 其余 `logger.error`） |

---

## 8. 已知约束

**name 契约无运行时校验**：`Detector.name` 和 `TemporalAnalyzer.name` 必须相同，否则 `get_slide_window()` 返回空列表，分析器静默失效。当前依赖子类硬编码保证。如需增加安全性，可在 `_get_stage_configs()` 中加入启动时校验。
