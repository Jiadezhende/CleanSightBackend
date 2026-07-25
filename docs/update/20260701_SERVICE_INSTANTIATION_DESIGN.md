# 服务实例化与类型加载设计（v1，待审查）

> **变更状态**：设计草案（2026-07-01）　<!-- 第一版，待审查后定稿 -->
> **知识库**：定稿后沉淀
>
> 背景：`ai.py` facade 清理时暴露了「什么在什么时候被实例化/加载」缺乏统一约定，且删除死 import 后触发过一次 client↔inference 循环。本文梳理现状并提出规则。

## 目的

明确三件事，消除 import 期意外副作用：
1. 哪些是**单例**（模块级实例），哪些**只提供类型**（不实例化为单例）；
2. 每个单例**构造期**到底做什么、什么被**推迟**（惰性）；
3. 一条决定「单例放哪、何时构造」的规则。

## 四类「东西」

| 类别 | 语义 | 构造时机 |
|------|------|---------|
| **A. 模块级单例（eager）** | 类 + 一个实例，随包 import 即构造 | import 包时 |
| **B. leaf-lazy 单例** | 类 + 一个实例，放独立 leaf 模块，**显式 import 才构造** | 消费方 import leaf 时 |
| **C. DI 类** | 只在包里给类；实例在**装配点**构造并注入依赖 | 装配点（router / 未来 composition） |
| **D. 类型 / 契约** | 不实例化为单例；按 run / 内部 / 每消息实例化 | 运行时按需 |

## 当前清单

### A. 模块级单例（eager，随包 import 构造）

| 实例 | 类 | 位置 | 经何处导出 |
|------|----|----|-----------|
| `client_manager` | `ClientManager` | [client/manager.py:332](../../app/services/client/manager.py#L332) | `client/__init__` |
| `stream_service` | `StreamService` | [stream/service.py:688](../../app/services/stream/service.py#L688) | `stream/__init__` |
| `persistence_manager` | `PersistenceManager` | [persistence/__init__.py:15](../../app/services/persistence/__init__.py#L15) | 就在 `__init__` |
| `run_controller` | `RunController` | [run_control.py:91](../../app/services/run_control.py#L91) | 模块内 |

### B. leaf-lazy 单例

| 实例 | 类 | 位置 | 为何 leaf |
|------|----|----|----------|
| `inference_manager` | `InferenceManager` | [inference/instance.py](../../app/services/inference/instance.py) | 构造有重副作用（见下），不应随 `import app.services.inference.*` 触发 |

### C. DI 类（实例在装配点建）

| 类 | 位置 | 实例化点 |
|----|------|---------|
| `GlobalHealthMonitor` | [health_monitor/monitor.py](../../app/services/health_monitor/monitor.py) | [routers/health.py:55](../../app/routers/health.py#L55)，构造注入 `client_manager / stream_service / inference_manager` |

### D. 类型 / 契约（非单例）

- 每 client：`ClientQueues`；每流：`FFmpegDecoder`；每 run：`ClientTemporalActor`、`Operator` 实现；每 stage N 个：`Detector`/`YOLODetector`；每帧/消息：`FrameInference`、`DetectionTask` 等 dataclass。
- **单实例但由 `inference_manager` 内部持有**（非模块单例）：`ModelWorkerService`、`MultiModelWorkerPool`、`VisualizationWorkerPool`、`StageFactory`。

## 构造期成本与惰性边界

**每个单例构造期实际做的事：**

| 单例 | 构造期做什么 | 线程 / IO |
|------|------------|-----------|
| `client_manager` | 初始化 dict / 锁 | 无 |
| `stream_service` | **构造即起 selector daemon 线程**（POSIX，[service.py:85-88](../../app/services/stream/service.py#L85)） | 起 1 线程 |
| `persistence_manager` | 建 `Queue` + pool 对象 | 无（线程在 `start()`） |
| `inference_manager` | 读 `inference_config.yaml`（**缺失 fail-fast**）+ 建 detector/operator/worker 对象图 + `mkdir` 存储目录 | 文件 IO + mkdir，**无线程、无权重** |
| `run_controller` | 空构造 | 无 |

**推迟到构造之后（惰性）：**

| 资源 | 触发点 |
|------|-------|
| **YOLO 权重** | **首次推理** `_ensure_model_loaded`（[detector.py:120](../../app/services/inference/detection/detector.py#L120)，:176/:184 调用），双重检查锁 |
| worker 线程（model / viz / persistence / actor） | 各自 `.start()` |
| per-run 组件（decoder / actor / operator） | run 启动时 |

> 更正备查：YOLO 权重**不在**任何构造/import 期加载，只在首次推理。`inference_manager` 构造的重点副作用是**配置 IO + fail-fast + 对象图 + mkdir + 触达 persistence/client**，这才是它放 leaf 的理由。

## 规则（本设计提出）

1. **廉价、无 IO 无线程的构造** → 单例放包根，eager（随 import 构造）可接受。（`client_manager`）
2. **构造有重副作用**（重 IO / fail-fast / mkdir / 跨服务触达 / 可能成环） → **leaf 模块 + 惰性**，只在消费方显式 import 时构造。（`inference_manager`）
3. **依赖需在装配点注入 / 由外部决定** → **DI 类**，不做全局单例。（`GlobalHealthMonitor`）
4. **纯数据 / 行为契约** → 放 `domain/` 或包内类，**永不单例化**。
5. **最重资源（线程、模型权重）一律 lazy** → `.start()` 或首次使用触发，**绝不在 import 或构造期**。
6. **跨域 import 若成环** → 懒加载破环（把顶层 import 挪进使用点），如 `queues.get_signals_10s` 内 import `inference.naming`。

## 已知不一致 / 待决（请审查）

1. **`stream_service` 构造即起 selector 线程**（import 副作用）——与规则 5「线程不在 import 期起」冲突。是否可接受，还是把 selector 线程挪到显式 `start()`？（现状：`stream/__init__` 一被 import 就有一个后台线程）
2. **`inference_manager` 构造期 eager 读 YAML（fail-fast）**——符合「配置要早失败」直觉，但也是 import 期文件 IO。判定：可接受，记录在案（这正是它必须是 leaf、不能进包 `__init__` 的原因）。
3. **`persistence_manager` 在 `persistence/__init__`**、`stream_service` 在 `service.py` 尾、`client_manager` 在 `manager.py` 尾——放置位置不统一（包根 vs 类模块尾），是否需要统一约定？（功能无差别，纯风格）
4. **`run_controller` ↔ `GlobalHealthMonitor` 接线点**：未来 HealthMonitor 的 `on_reclaim=run_controller.stop_run` 注入应发生在装配点（`routers/health.py` 或引入统一装配处），避免 service 反向 import router。

## 与当前重构的关系

- 本设计是 `ai.py → inference/instance.py` 清理的收尾说明；
- `run_controller` 为控制面协调单例（见 [RunController 计划](../../.claude/plans/)）；
- 规则 2/6 已在本轮落地（inference leaf + queues 懒 import 破环）。
