# 包结构与导入纪律规范：`__init__` 零副作用 + 单例出包、活体生命周期归包

> **变更状态**：生效中（提案 2026-09-03 → 期 1-3 全部落地 2026-09-04）
> <!-- 规范条文 §1-§8 已全部有代码兑现 + 门禁测试锁死；逐期实际改动见文末「落地记录」 -->
> **知识库**：待沉淀

## 概述

给 `app/services/<svc>/` 定一套明确的包结构规范：固定文件角色与子包三类、重依赖分级、
`__init__.py` 零副作用、活体单例与 lifespan 的归属、依赖注入三档边界、配置与静态资产的落点。
同时记录七个服务包对该规范的现状偏差与实测导入开销（`import app.main` 现为 1232ms 并拽入 torch）。
本篇前半是规范条文（§1-§8），后半「落地记录」是期 1-3 的实际改动——规范与它的兑现放在同一篇，
读条文时能直接看到对应代码落在哪。

## 变更背景

### 现状 / 痛点

**痛点一：`import` 开销失控，且与"重依赖懒加载"的既有意图不符。** 实测（`.venv`，冷进程各测一次）：

| 包 | import 耗时 | 拽入的重依赖 |
|------|-----------|------------|
| `app.domain` | 0.8 ms | — |
| `app.services.inference` | 224 ms | numpy |
| `app.services.stream` | 225 ms | numpy |
| `app.services.persistence` | 315 ms | numpy, **cv2** |
| `app.main` | **1232 ms** | numpy, sqlalchemy, **cv2**, **torch** |

torch 的引入路径已定位：`routers/ai.py` → [`inference/instance.py`](../../app/services/inference/instance.py)
建单例 → `InferenceManager.__init__` → [`manager.py:113`](../../app/services/inference/manager.py) `_get_stage_configs()`
→ `stage_factory._import_class` → `importlib` 加载 [`temporal/impl/clean.py:11`](../../app/services/inference/temporal/impl/clean.py)
的顶层 `import torch`。即**单例在 import 期就把全部 impl 与 torch 拉起**。

cv2 的引入路径：`persistence/__init__.py` 顶层 re-export `manager` → `strategies` →
[`hls_strategy.py:27`](../../app/services/persistence/strategies/hls_strategy.py) 顶层 `import cv2`。

后果直接落在测试上——只想取一个 dataclass 的用例被迫付全额：

```
from app.services.persistence.models import HLSPersistenceTask   # 458ms, cv2 已加载, manager 实例已构造
from app.services.inference.models   import DetectionTask        # 198ms, 无 torch, 单例未构造
```

差异的机制是：**导入包内任一子模块都会先执行该包的 `__init__.py`**，所以写在 `__init__.py`
模块级的一切（含 `persistence_manager = PersistenceManager()`）是包里每个消费者的过路费。
`lifespan()` 救不了这一点——它是函数，body 要到 `main.py` 的 `async with` 才执行，而构造发生在 import 期，
两个时刻隔着整个进程启动。

**痛点二：七个服务包的结构不齐，且偏差集中在生命周期归属上。**

> ⚠️ 下表是**落地前的审计快照**，表内每一处加粗都已在期 1-3 消掉，现状见文末「落地记录」。
> 保留它是为了记住偏差长什么样。

| 服务 | 活体持有者 | 单例定义处 | lifespan 在哪 | 配置 | 私有数据形状 |
|------|-----------|-----------|--------------|------|------------|
| `persistence` | `manager.py` | `__init__.py:17` | ✅ 包内 `__init__.py` | `config.py` | **`models.py`** |
| `inference` | `manager.py` | ✅ `instance.py:14` | ❌ `routers/ai.py` | `config.py` | **`models.py`** |
| `client` | `manager.py` | `manager.py:237` | —（无需起停） | `config.py` | `queues.py` 兼 |
| `stream` | **`service.py`** | `service.py:458` | ❌ `routers/ai.py` 的 `finally` | `config.py` | 无 |
| `health_monitor` | **`monitor.py`** | ⚠️ **`routers/health.py` 的 global** | ❌ `routers/health.py` | `config.py` | ✅ `types.py` |
| `lab` | 无（纯方法） | — | — | **`runtime_config.py`** | 无 |
| `traceback` | 无（纯方法） | — | — | 无 | 无 |
| `run_control` | **裸模块，非包** | `run_control.py:245` | — | — | — |

> 加粗 = 偏离本规范；✅ = 已是规范样板。注意"私有数据形状"一列的判定方向：本规范取
> **`types.py`**（理由见 §1），故 `health_monitor/types.py` 是正确样板，`inference` / `persistence`
> 的 `models.py` 才是待收敛项。

其中两项有实际代价：

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | `health_monitor` 实例住在 [`routers/health.py:47`](../../app/routers/health.py) 的 `global _health_monitor`，包内无单例；想拿它得 `from app.routers.health import get_health_monitor` | services 反向依赖 routers，是当前分层里唯一的真环 |
| #2 | `stream` / `inference` 的 lifespan 都在 `routers/ai.py`，其中 `stream_service.shutdown()` 寄生在 `ai.lifespan()` 的 `finally`、且无对称 `start()` | 协议层持有服务生命周期；调整 `ai` 的嵌套位置会静默改掉 stream 的关停时机 |

其余偏差（`manager.py`/`service.py`/`monitor.py` 命名、`config.py`/`runtime_config.py`、
`models.py`/`types.py`）只影响查找成本，不影响正确性，列为 P2。

### 触发来源

架构评审讨论：既有意图是"重依赖懒加载以便测试、协作者可注入以便测试"，但从未写成条文，
导致同一意图在七个包里有七种实现程度。实测后确认意图在 `persistence` 与 `app.main` 上未兑现。

### 承接

本次建立在 [20260802 推理 impl 按契约包归位](20260802_INFER_IMPL_RELAYOUT.md) 之上——
该次把业务 impl 收进各契约包的 `impl/` 子层、只经 `stage_factory` 的 `importlib` 按配置加载。
正是这一层间接使得 `import app.services.inference` 至今不含 torch；本规范把当时的隐式收益
显式化为条文（§2 通路 1）并加门禁保护。

## 方案详情

### 方案选型：模块级单例是否该被替代

`run_control` / `health_monitor` / worker 线程 / spawn 子进程等**主要消费者都不在 HTTP 请求上下文里**
（[`run_control.py:20-25`](../../app/services/run_control.py) 顶层 import 四个单例，而 `start_run`
由 `asyncio.to_thread` 与 HealthMonitor 后台线程调用），这一事实排除了多数替代方案：

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **模块级单例 + `instance.py`（采用）** | 无新增机制；风险在"谁都能 import"，由 §6 门禁约束 | 采用。唯一能同时覆盖请求路径与线程/子进程的形态 |
| `app.state` + `Depends` | 只覆盖 HTTP 路径 | 否。线程与子进程够不着 → router 走 DI、线程走单例的双轨，比现状更糟 |
| DI 容器（`dependency-injector` 等） | 引入间接层与学习成本 | 否。活体仍是进程唯一，容器收益在"整棵替换"，而测试本就不该起活体 |
| `contextvars` | — | 否。它管请求级值（[`utils/context.py`](../../app/utils/context.py) 已用于 client_id），与进程级活体正交，非替代关系 |
| lifespan 构造 + 全程显式传递 | RunController 需 4 个、HealthMonitor 需 3 个协作者，参数一路下穿 | 否。且 worker 线程仍拿不到 |
| 惰性 accessor `get_x()` | 多一层间接 + `set_x()` 后门的诱惑 | 否。其唯一收益是"构造推迟到首次使用后再读 settings"，而 §4 令构造零副作用后该收益归零 |

**结论：不换机制，换约束。** 测试杠杆也不在单例机制上——测试要的是"自己配好的实例"（§5 第二档构造注入），
而非"替换那个全局实例"，全局单例怎么定义对它无所谓。

### 规范条文

#### §1 包内文件角色（固定文件名，不自创）

```
app/services/<svc>/
  __init__.py     公开面：docstring +（有活体时）lifespan()。零业务逻辑、零重依赖
  instance.py     模块级单例，唯一定义处
  config.py       配置读取与默认值。只依赖 app.settings，不依赖同包其他模块
  types.py        服务私有 dataclass。只依赖 app.domain + stdlib
  manager.py      活体持有者：类定义 + start/stop + 对外方法
  <capability>.py 无状态纯方法模块
  impl/           可插拔实现，只经 importlib 按配置加载
  workers/        线程 / 进程体
```

文件名即依赖上界：`config.py` 里出现 `from .manager import ...` 就是错的。

> **为什么是 `types.py` 而不是 `models.py`**：本仓库 [`app/models.py`](../../app/models.py) 的 docstring
> 已声明"只放 ORM"——`models` 这个名字在本代码库里已被"DB 行映射"占用。再让
> `services/*/models.py` 表示"进程内 dataclass"属同名不同义，读者看到 `models` 无法判断是哪一种。
> `types.py` 无歧义；Python 3 绝对导入下 `from .types import X` 也不会遮蔽 stdlib `types`。
> `health_monitor/types.py` 是现有的正确样板；`inference` / `persistence` 的 `models.py` 已于期 3 收敛。

**子包角色三类**（多子包的服务按此归类，`inference` 是唯一样板）：

| 类别 | 形状 | 实例 |
|------|------|------|
| **契约包** | 顶层基类 + 框架管件 + `impl/` 子层，三包对称 | `detection/`（Detector, L1）、`temporal/`（Operator, L3/L4）、`offline/`（Segmenter） |
| **基础设施包** | 无基类、无 `impl/`，提供落盘/工具能力 | `feature/`（FeatureStore / FactLedger） |
| **活体包** | 由 manager 持有的 worker 池 | `visualization/`（pool / worker / visualizer） |

三类不可混谈——契约包的"基类 + impl/ 对称"是 20260802 归位时定下的结构，
基础设施包与活体包本就不该有 `impl/`，不是"没做完"。

> **`cli.py` 例外条款**：服务包内允许有 `cli.py` 作为 `python -m` 离线/运维入口
> （[`offline/cli.py`](../../app/services/inference/offline/cli.py) 即
> `python -m app.services.inference.offline.cli run|query`），但它是**单向出口**——
> **不得被包内任何其他模块 import**。现状零反向引用，符合。它不违反"进程入口在顶层 `main.py`"，
> 因为那条约束的是**常驻服务进程**；一次性命令行工具就近放在它操作的服务包里。

#### §2 依赖分级与重依赖的三条通路

| 级 | 内容 | 可否模块顶层 import |
|----|------|------------------|
| L0 | stdlib、dataclass、`app.domain` | 任何地方 |
| L1 | numpy、pydantic | 任何地方（numpy 已在 `app.domain` 里，躲不掉也不必躲） |
| L2 | **torch / ultralytics / cv2** | **禁止**，除非在 `impl/` 或 `workers/` 下 |
| L3 | 有副作用的：`app.database`（建连接池）、`app.settings`（读环境） | 禁止在被广泛 import 的模块顶层 |

L2 只有三条合法通路：

1. **`impl/` 下允许顶层 import** —— 它只经 `stage_factory._import_class` 的 `importlib` 加载，代价延迟支付。
   **代价条款：`impl/` 不得被任何 `__init__.py` re-export**，一旦 re-export 立即退化为 eager。
2. **函数体内 import** —— 如 [`temporal/operator.py:120`](../../app/services/inference/temporal/operator.py)。
   用于 `impl/` 之外确需 torch 的地方。
3. **`workers/` 下、且是 spawn 子进程 target 的模块** —— 反而有更严的额外约束：
   [`detection/stage_worker.py`](../../app/services/inference/detection/stage_worker.py) 模块 docstring 载明
   "顶层不 import torch，否则早于 `run_stages` 钉 `CUDA_VISIBLE_DEVICES`"，这是硬正确性约束而非性能偏好。

#### §3 `__init__.py` 两种形态，禁止中间态

- **门面型**（包内有活体）：docstring + `lifespan()` + 少量轻类型导出。
- **标记型**（无活体）：纯 docstring，不 re-export，消费方走深路径。样板是
  [`temporal/__init__.py`](../../app/services/inference/temporal/__init__.py)——其"纯包标记不做 re-export"
  正是 torch 未被 `import app.services.inference` 拽入的直接原因。
- **禁止中间态**：re-export 一大堆便利符号却无活体。唯一效果是把整棵子树的重依赖变 eager，收益仅是少打几个点。

**`__init__.py` 模块级只允许 import 轻量类型与 `contextlib`；一切指向 `instance` / `manager` / `impl` 的
import 必须写在函数体内。** 这条是整个模式的开关：

```python
# __init__.py
@asynccontextmanager
async def lifespan():
    from .instance import inference_manager      # ← 写到文件顶部则前功尽弃
    inference_manager.start()
    try:
        yield
    finally:
        inference_manager.stop()
```

#### §4 单例只挂名，不干活

> **模块级单例的 `__init__` 只允许赋值和建空容器。任何"干活"——读配置文件、`importlib` 加载 impl、
> `mkdir`、连 DB、建线程池——一律推迟到 `start()` 或首次使用。**

`InferenceManager.__init__` 当前做了 `mkdir`、建 `VisualizationWorkerPool`、`_get_stage_configs()`
（→ 加载全部 impl → torch），三个后果：`--reload` 每次改动多等一秒；任何 import 到 router 的单测
都要付 1.2s 与一份常驻 torch；构造出错时表现为 `ImportError` 而非启动失败，栈难读。

#### §5 `instance.py` 的职责：把「类」和「那一个实例」分开

三个文件三件事，互不重复：

| 文件 | 唯一职责 | 谁付代价 |
|------|---------|---------|
| `manager.py` | **类定义** | 想要类的人（含测试自己 new 一个带 mock 的） |
| `instance.py` | **那一个全局实例** | 只有明确要全局单例的人（router、lifespan body） |
| `__init__.py::lifespan()` | **起停编排** | 谁都不付（函数体延迟） |

单例写在 `manager.py` 末尾（client / stream 现状）时，测试 `from ...manager import InferenceManager`
想自造实例，**拿到类的同时也把全局单例造出来了**，连同它 `__init__` 里的一切。分出 `instance.py` 后这条路才干净。

> 一句话判据：**`import app.services.<svc>` 必须零副作用、零重依赖**；要活体就显式 `from .instance import x`。

#### §6 依赖注入三档 + 单例引用面

| 对象 | 做法 |
|------|------|
| 活体单例（`inference_manager` / `persistence_manager` / `client_manager` / `stream_service` / `run_controller`） | **模块级单例，不注入**。测试不碰——真要碰说明该测的是里面的 seam |
| 有外部 I/O 的协作者（DB session、`LabelStudioClient`、`FeatureStore`、下游 service） | **构造注入 + 生产默认值**：`def __init__(self, store=None): self._store = store`，`None` 时在 `start()` 里按 settings 建或取全局单例。生产端零改动，测试传假的 |
| 纯方法模块 | **不注入**。要替换的是入参数据，不是依赖 |

三条禁令：不引入 DI 容器；`Depends()` 只用于请求级对象（DB session、当前 token），绝不注入活体服务；
不给单例加 `set_instance()` / `reset()` 后门（要替换活体说明被测逻辑粘在活体上了，正解是抽 seam）。

**单例引用面（把 [`run_control.py`](../../app/services/run_control.py) docstring 里的软约定变硬）：**

> 单例只允许被三类模块 import：`run_control`（编排中枢）、`routers/*`（装配层）、单例自己包内的 `lifespan()`。
> **服务与服务之间不得直接 import 对方单例。**

现状符合度（落地后）：`run_control` 顶层拿四个（✅ 这正是它存在的意义）；`health_monitor` 三个协作者
走构造注入，缺省者在 `start()` 的 `_resolve_deps()` 里现取
（✅ [`health_monitor/manager.py`](../../app/services/health_monitor/manager.py)）；同文件函数内
`from app.services.run_control import run_controller` 反向指回编排中枢（⚠️ **具名例外之一**，
函数内 import 说明作者已察觉环）。另一处具名例外是
[`inference/temporal/alarm_sink.py`](../../app/services/inference/temporal/alarm_sink.py)：
inference 产告警 → persistence 落库，跨服务但方向正确（下游依赖），sink 是这条方向唯一的窄接口。
两处均已写进门禁的 `SINGLETON_EXCEPTIONS` 并注明理由。

`client_manager` **不受本条约束**：它是零跨服务依赖的中台 leaf，谁都可以向下依赖它，
限制它的引用面没有意义——故它不在门禁的 `SINGLETONS` 表里。

#### §7 门禁（唯一硬指标）

[`tests/test_import_hygiene.py`](../../tests/test_import_hygiene.py)（已落地，7 个用例）：
**必须起子进程**——pytest 主进程早被别的用例把 torch/cv2 装进 `sys.modules` 了，在本进程里测等于没测。

```python
HEAVY = ("torch", "ultralytics", "cv2")   # 被盯防的重依赖
BUDGET = {                                # (模块, 允许出现的重依赖集合, 耗时上限秒)
    "app.domain":               (set(), 0.20),
    "app.services.client":      (set(), 1.0),
    "app.services.inference":   (set(), 1.0),
    "app.services.persistence": (set(), 1.0),
    "app.main":                 (set(), 2.0),
}
```

> **与提案版的差异**：提案里 allowed 集合写的是 `{"numpy"}` / `{"numpy","sqlalchemy"}`。实现时把
> numpy / sqlalchemy 整体移出 `HEAVY`，allowed 一律留空。理由：这两个是 L1/L3、本来就到处都在用，
> 放进 `HEAVY` 只会让每个模块都得申报一次白名单，噪声大于信号；门禁只盯 L2（torch/ultralytics/cv2）
> 这条真正会失守的线。耗时上限按实测取 ~3-5× 余量，只兜「量级失守」，不做性能回归。
> `app.domain` 上限从 0.05 放宽到 0.20——实测 1 ms，但 0.05 在冷缓存/负载下会偶发红。

耗时断言**没有**标 `@pytest.mark.slow`：本仓库没有任何 pytest 配置文件（无 pytest.ini / setup.cfg /
pyproject），加自定义 marker 会每次跑出 unknown-marker 警告。改为硬断言 + 大余量。

另两条 AST 门禁（遍历 `app/` 全部 `.py` 的 import）：

- `test_singleton_reference_surface` —— §6 的引用面。允许方由规则表达（`run_control.py` /
  `routers/*` / 任意 `__init__.py`）+ 两条具名例外。它同时守住 [DEVELOPMENT.md](../DEVELOPMENT.md) §3
  已写下但此前无人检查的"不建 service 对 service 的直接依赖"。
- `test_services_do_not_import_routers` —— 锁死期 1 消掉的那个真环，防回潮。

一条测试顶十页文档：新增模块只要在任一包的 `__init__` 链上顶层 import L2 依赖，这条即红。

#### §8 配置与静态资产的落点

**顶层 `config/` 的位置判定为合理，保持不动。** 五份 `<svc>_config.yaml` 是运维要改的东西，
不该埋进 Python 包；放顶层才能在部署时整目录覆盖或挂载。

问题不在位置，在**路径解析**：`client` / `stream` / `inference` / `persistence` / `health_monitor`
五个 `config.py` 各自写

```python
base_dir = Path(__file__).parent.parent.parent.parent.resolve()
config_path = base_dir / "config" / "<svc>_config.yaml"
```

数四层目录，五处重复；任一 `config.py` 换深度就默默指错。而
[`persistence/config.py:41`](../../app/services/persistence/config.py) 的注释已经载明存储路径
"已上移到 `settings.storage_base_dir`（单一真源），不在此定义"——**同一个文件里两种做法并存**。

> **条款**：配置目录路径收敛到 `settings.config_dir`（照 `settings.storage_base_dir` 的写法），
> `config.py` 不再自数层级。这与 §1「`config.py` 只依赖 `app.settings`」同向。

**静态资产**：[`app/static/`](../../app/static/) 下 6.2 MB 前端 vendor 入库是**刻意决策**——
[`.gitignore:32-34`](../../.gitignore) 反向放行并注明"部署机靠 git archive 取码，必须跟踪"，
符合离线分发原则，不动，也不移出 `app/`（`static/` 不含 `.py`，不影响 import 面）。

真问题是 [`main.py:106-107`](../../app/main.py) 用 **CWD 相对路径**挂载：

```python
app.mount("/admin-f3m8/ui", StaticFiles(directory="app/static/admin", html=True), ...)
```

而 `start_backend.sh` 并不 `cd` 到仓库根，只是默认从根被调用。这是全仓唯一没走 `__file__` 推导的
路径解析——换目录启动即 404。

> **条款**：静态资产挂载路径必须由 `__file__` 推导，不得用 CWD 相对路径。

### 保留项（刻意不改）

- **`lab` / `traceback` 不补 manager/config/types 五件套**——它们是纯方法包，补齐是纯负担。
- **`client` 不加 lifespan**——它是哑存储，没有要起停的活体。
- **`inference/instance.py` 不"统一"掉**——它是最优形态，该统一的方向是 `persistence` 向它靠。
- **`run_control.py` 暂不升为包**——245 行、职责单一，涨到需要拆文件时再说。
- **模块级单例机制本身不动**（见方案选型）。
- **顶层 `config/` 与 `app/static/` 的位置都不动**（见 §8），`app/models.py`（ORM）也不改名——
  §1 把服务私有数据形状改叫 `types.py`，正是为了把 `models` 这个名字还给 DB 行映射。
- **`app/static/` 的 vendor 不去重**：`admin/vendor/` 与 `lab/vendor/` 各存一份
  `element-plus.css` / `element-plus.full.js` / `hls.js` / `vue.global.prod.js`（4 个文件字节数完全相同，
  约 3.1 MB 重复）。合并需改两个 `index.html` 的 `script src` 并新增第三个 mount 点，
  收益仅是仓库体积——本轮不做。**记此条防后人误当遗漏。**
- **`inference` 子包不做物理重组**：5 个子包按 §1 的三类归位已经清楚，20260802 刚归位过一轮，
  再加 `contracts/` / `infra/` 中间目录会让所有深路径 import 变长一级，无实质收益。
- **`FactLedger` 不从 [`feature/store.py`](../../app/services/inference/feature/store.py) 拆出**：
  它的消费方确实全在 `offline/`（`runner.py:24`、`cli.py:63`），在线侧零引用，看似该按
  online/offline 分离挪走；但它与 `FeatureStore` 共享同一套 `(task_id, step_id)` 分区键与 JSONL
  落盘约定，拆开会把落盘格式真源劈成两个文件。保持同居，靠
  [`feature/__init__.py`](../../app/services/inference/feature/__init__.py) 的 docstring 区分二者生命周期。
- **`app/services/temp/colorstrip/` 不在本规范辖域**：它是实验脚手架（无 `__init__.py`，目录内含
  `samples/` / `out/` / `golden.json` / `REPORT.md`），不是服务包，上述条款一概不适用于它。
  是否移出 `app/services/` 另行决策，本轮不动。

## 变更效果

期 1-3 已全部落地，下表右列为**实测**（同机同一轮，`.venv` 下冷进程逐模块 import 计时）：

| 维度 | 落地前 | 落地后（实测） |
|------|------|--------------|
| `import app.main` | 1232 ms，含 **torch + cv2** | **407-492 ms**（三次），只余 numpy/sqlalchemy |
| `import app.services.persistence.types` | 458 ms，含 **cv2**，manager **已构造** | **318 ms**，无 cv2，`persistence.manager` / `.instance` 均**未加载** |
| `import app.services.inference.types` | — | 269 ms，无 torch |
| `import app.domain` | 0.8 ms | 1 ms |
| 服务 lifespan 归属 | 1/3 在包内，2/3 在 `routers/` | **全部在包内**，`main.py` 只做四层嵌套 |
| services → routers 反向依赖 | 1 处（`health_monitor` 实例） | **0**（由 `test_services_do_not_import_routers` 锁死） |
| 配置目录路径解析 | 5 处各自 `__file__` 数四层 | 统一 `settings.config_dir` |
| 静态资产挂载 | CWD 相对路径，换目录启动即 404 | `__file__` 推导，与全仓一致 |
| 私有数据形状文件名 | `models.py` ×2 / `types.py` ×1，与 ORM 同名 | 统一 `types.py`，`models` 归 ORM 专用 |
| 规范可执行性 | 全为文字约定，无检查 | 3 类门禁共 7 个用例 |

> `app.main` 的绝对值比提案预估的 ~300 ms 高一档：剩下的 400 ms 几乎全是 numpy + sqlalchemy +
> fastapi/pydantic 自身的固定成本，属 L1/L3 的合理开销，不是残留的 L2 泄漏（`HEAVY` 集合实测为空）。

**自测结果**：

- `pytest tests/` **451 passed**（落地前基线 444，新增 7 个门禁用例），36.7 s，零 warning 变化。
- 期 2.1 的 cv2 泄漏由 `builtins.__import__` 打桩定位到
  [`inference/manager.py`](../../app/services/inference/manager.py) 顶层的
  `from ...visualization.pool import VisualizationWorkerPool`——改 `TYPE_CHECKING` + 函数体内 import
  后 `app.main` 从 1232 ms 掉到 412 ms 且 torch/cv2 双双消失。
- 期 3.1 的直接验证点：从**非仓库根目录**（`/tmp`）起进程，确认两个 `StaticFiles` mount 的 directory
  均指向真实目录，且 `get_client_config()` / `load_stage_config()` 能正常读到 yaml。
- **未做**：dev 环境端到端启停一轮（验证四层 lifespan 的起停序与无 "Error stopping"）。
  该项会连真实 DB，按 [CLAUDE.md](../../CLAUDE.md) 需先与人确认，**留作合入前的必做项**（见下）。

## 落地记录

三期均已完成。期 3 是纯收敛（改名 + 改路径，零逻辑改动），建议与期 1/2 分开提交。

### 期 1：消环 + lifespan 归位

| 文件 | 改动 |
|------|------|
| [`health_monitor/instance.py`](../../app/services/health_monitor/instance.py) | 新建。单例从 `routers/health.py` 的 `global _health_monitor` 迁回包内 |
| [`health_monitor/manager.py`](../../app/services/health_monitor/manager.py) | 四个入参一律可缺省；新增 `_resolve_deps()`（配置与三个协作者推迟到 `start()` 现取）与 `is_running` 属性；`stop()` 加空转保护 |
| [`health_monitor/__init__.py`](../../app/services/health_monitor/__init__.py) | 加 `lifespan()`；DEBUG 配置日志与停机 stats 日志从 router 迁入 |
| [`routers/health.py`](../../app/routers/health.py) | 删 `global _health_monitor` 与 `lifespan()`；三个端点的 `if _health_monitor is None` 改为 `if not health_monitor.is_running` |
| [`stream/instance.py`](../../app/services/stream/instance.py) | 新建（从 `service.py:458` 迁入单例） |
| [`stream/__init__.py`](../../app/services/stream/__init__.py) | 加 `lifespan()`：**start 段为空**（decoder 懒启动、由 `run_control` 按 run 起），只在 `finally` 收尸 |
| [`inference/__init__.py`](../../app/services/inference/__init__.py) | 加 `lifespan()`（从 `routers/ai.py` 迁入） |
| [`routers/ai.py`](../../app/routers/ai.py) | 删整个 `lifespan()`（含寄生在 `finally` 的 `stream_service.shutdown()`）及连带的顶层 import |
| [`main.py`](../../app/main.py) | 改四层嵌套 `health → stream → persistence → inference`，保持 persistence 先起后停 |

**`is_running` 是这期唯一的新语义**：router 原先靠 `_health_monitor is None` 判断"监控没起"，单例出包后
实例恒非 None，改判线程活性（未 start / 已 stop / 线程已死 都算 False）——这三态在旧写法下是同一态。

### 期 2：导入瘦身 + 门禁

| 文件 | 改动 |
|------|------|
| [`inference/manager.py`](../../app/services/inference/manager.py) | `__init__` 只留赋值；`mkdir` / `VisualizationWorkerPool` / `FeatureStore` / `_create_async_model_worker_service()` 全部移入新增的 `_build_components()`（由 `start()` 调，幂等）。`DetectionService` / `VisualizationWorkerPool` 的类型标注改 `TYPE_CHECKING` + 字符串标注——**这一处是 torch/cv2 的真正泄漏点** |
| [`persistence/instance.py`](../../app/services/persistence/instance.py) | 新建（单例从 `__init__.py:17` 迁入） |
| [`persistence/__init__.py`](../../app/services/persistence/__init__.py) | 删全部顶层 re-export（cv2 的传播路径），只留 docstring + `lifespan()`，单例 import 写进函数体 |
| [`persistence/strategies/hls_strategy.py`](../../app/services/persistence/strategies/hls_strategy.py) | 顶层 `import cv2` 移入 `_persist_raw_segment` / `_persist_processed_segment` 两个函数体 |
| [`tests/test_import_hygiene.py`](../../tests/test_import_hygiene.py) | 新建，7 个用例（见 §7） |

### 期 3：路径与命名收敛

| 改动 | 详情 |
|------|------|
| `settings.config_dir` | [`settings.py`](../../app/settings.py) 新增属性（照 `storage_base_dir` 写法）；五个 `services/*/config.py` 的 `Path(__file__).parent×4` 全部换掉 |
| 静态资产 | [`main.py`](../../app/main.py) 两处 mount 改 `_STATIC_DIR = Path(__file__).parent / "static"` |
| 改名（`git mv`，保留历史） | `inference/models.py` → `types.py`；`persistence/models.py` → `types.py`；`stream/service.py` → `manager.py`；`health_monitor/monitor.py` → `manager.py`；`lab/runtime_config.py` → `config.py`。`health_monitor/types.py` 不动（已是样板） |
| import 跟进 | 脚本批量重写 `app/` `tests/` `integration_tests/` `scripts/`，共 **27 个 .py 文件**；`routers/lab.py` 因原是 `from app.services.lab import runtime_config` 形式，改为 `import config as lab_config` 并跟进 10 处调用点 + 1 处测试 monkeypatch 目标 |
| docstring | `inference/__init__.py` 的子包分类段按 §1 三类（契约包 / 基础设施包 / 活体包）重写，并补 `offline/cli.py` 不得被包内 import 的条款 |

**`lab_runtime_config.json` 不改名**：那是落盘文件名，改了会让已部署环境的持久化配置成孤儿。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **dev 启停一轮未做** | 四层 lifespan 的实际起停序、期 2 重活推迟后推理链路是否仍正常，目前只有单测覆盖 | 合入前必做：起 dev 确认起停日志为 health→stream→persistence→inference / 逆序，且无 "Error stopping"。**会连真实 DB，跑前先与人确认** |
| `stream` 关停时机比落地前更早（原寄生在 `ai.lifespan` 的 `finally`，现为 persistence 外层） | 理论上可能出现"流已停、persistence 还在写残段"的报错 | 同上，在 dev 启停中重点看这一段日志 |
| [`docs/kb/`](../kb/) 与 [`docs/api/health.md`](../api/health.md) 仍写旧模块路径（`monitor.py` / `service.py` / `*.models`） | 查文档按图索骥会扑空 | 按约定 KB 只在维护流程里更新，本轮不动；**此条即是给 KB 融合的输入** |
| 门禁耗时上限在不同机器/冷热缓存下波动 | 测试偶发红 | 已取实测 ~3-5× 余量；重依赖断言是硬条件，耗时断言只兜量级失守。真偶发就放宽上限，别删断言 |
| 新服务包若不遵守 §3 两种形态，门禁只在它泄漏 L2 依赖时才红 | 结构漂移无检查 | 已接受：形态本身难以静态判定，靠 review + 本篇规范 |
