> 更新时间：2026-09-20
> 依据来源：代码分析（`app/` 全量 + `tests/test_import_hygiene.py`）
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 包分层与导入纪律

`app/` 下有五个层级，依赖**单向向下**。本文回答两个问题：**哪一层能 import 哪一层**，以及
**一个服务包内部长什么样**。落盘布局与存储层准入判据不在这里，见
[DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)。

## 1. 五层依赖图

```text
routers/          装配层：HTTP 协议、token 签发、URI 拼装、DTO
   │
services/<svc>/   业务服务，单向依赖下面几层
   │
services/utils/   服务层工具：无状态纯函数，多个 service 都要、但不属于任何一个
   │              **不得 import 任何兄弟 service 包**
   ├──────────────┐
   ▼              ▼
storage/        utils/       数据层 / 基建，两个平行的 leaf
   │              （异常、执行器、指标、SerialTaskQueue、网关）
   ▼
domain/         内存数据契约（Frame / Detection / FrameFeature / Alarm / RenderSpec）
                纯 dataclass，零服务依赖，是整棵树的叶子
```

`app/settings.py` 与 `app/database.py` / `app/models.py` 不在这条链上：`settings` 谁都可以读
（有副作用，故不在被广泛 import 的模块顶层）；`database` / `models` 是 ORM，**数据层与
`services/utils/` 一律不许碰**。

三条边由门禁测试锁死，不是靠自觉：

| 门禁用例（`tests/test_import_hygiene.py`） | 守什么 |
|---|---|
| `test_layer_package_imports_only_whitelisted_app_modules` | `app/storage` 与 `app/services/utils` 的 `app.*` 白名单 |
| `test_services_do_not_import_routers` | services → routers 的反向环 |
| `test_singleton_reference_surface` | 单例引用面（§4） |

### 两个 leaf 包的白名单

```text
app/storage        → app.storage, app.domain, app.settings
app/services/utils → app.services.utils, app.storage, app.domain, app.utils, app.settings
```

白名单而非黑名单：`app/storage` 能同时被写侧（recording）与读侧（traceback / lab /
inference.offline / routers）依赖的前提，是**它谁都不依赖**。黑名单只挡得住 `app.services.*`，
挡不住 `app.database` / `app.models`——那两个进来不造环、不报错，只会在某天想换存储时才发现。

`app.services.utils` 这个前缀**不放行兄弟包**：检查是 `name == ok or name.startswith(ok + ".")`，
`app.services.lab` 差的正是那个点。破了它，本包就成了 service → service 依赖的后门
（lab 想调 traceback 的东西，在这里加个转发函数就绕过去了，而单例门禁只盯单例、看不见转发）。

## 2. 重依赖分级：L2 只有三条合法通路

| 级 | 内容 | 可否模块顶层 import |
|----|------|------------------|
| L0 | stdlib、dataclass、`app.domain` | 任何地方 |
| L1 | numpy、pydantic | 任何地方（numpy 已在 `app.domain` 里，躲不掉也不必躲） |
| L2 | **torch / ultralytics / cv2** | **禁止**，除非走下面三条通路 |
| L3 | 有副作用的：`app.database`（建连接池）、`app.settings`（读环境） | 禁止在被广泛 import 的模块顶层 |

L2 的三条通路：

1. **`impl/` 下允许顶层 import**——只经 `stage_factory._import_class` 的 `importlib` 按配置
   加载，代价延迟支付。**代价条款：`impl/` 不得被任何 `__init__.py` re-export**，一旦
   re-export 立即退化为 eager。
2. **函数体内 import**——`impl/` 之外确需 cv2/torch 的地方。`app/storage/hls/_encode.py` 的
   cv2 就在 `write_mp4v` 函数体内。
3. **`workers/` 下、且是 spawn 子进程 target 的模块**——反而有更严的额外约束：
   `detection/stage_worker.py` 顶层不许 import torch，否则早于 `run_stages` 钉
   `CUDA_VISIBLE_DEVICES`，这是硬正确性约束而非性能偏好。

门禁只盯 L2（`HEAVY = ("torch", "ultralytics", "cv2")`）。numpy / sqlalchemy 刻意不在其中
——它们是 L1/L3、本来就到处都在用，放进去只会让每个模块都申报一次白名单，噪声大于信号。

### 导入预算逐模块登记

`BUDGET` 表登记 `(模块, 允许出现的重依赖集合, 耗时上限秒)`，**存储层每个模块逐个登记、不能
只登记包名**：包根是标记型 `__init__`、零 re-export，`import app.storage` 根本不加载任何域
文件，登记包名挡不住有人往域文件里塞 ffmpeg/cv2。新增域文件必须同时加一行，由
`test_layer_package_modules_are_all_budgeted` 强制。

⚠ **子包成员条目实际量的是整个 facade，不是它自己**：`hls/` 的 `__init__` 是 facade，import
任何 `app.storage.hls.X` 都会先跑包 `__init__` 并连带加载全部实现模块。所以 `_layout` /
`_m3u8` / `_read` / `types` 几条的实测值与 `app.storage.hls` 一模一样。那里注释写的
「stdlib only」是**源码事实，不是门禁的结论**——往 `types.py` 塞一行 `import numpy` 不会让
任何一条红。真正被这些条目守住的只有 HEAVY 三项。

## 3. 服务包内部结构

```text
app/services/<svc>/
  __init__.py     公开面：docstring +（有活体时）lifespan()。零业务逻辑、零重依赖
  instance.py     模块级单例，唯一定义处
  config.py       配置读取与默认值。只依赖 app.settings，不依赖同包其他模块
  types.py        服务私有 dataclass。只依赖 app.domain + stdlib
  manager.py      活体持有者：类定义 + start/stop + 对外方法（也叫 service.py / monitor.py）
  <capability>.py 无状态纯方法模块
  impl/           可插拔实现，只经 importlib 按配置加载
  workers/        线程 / 进程体
```

文件名即依赖上界：`config.py` 里出现 `from .manager import ...` 就是错的。

**私有数据形状叫 `types.py` 不叫 `models.py`**：`app/models.py` 已声明只放 ORM，`models` 这个
名字在本仓库被「DB 行映射」占用了。

**子包角色三类**（`inference` 是唯一有多子包的样板）：

| 类别 | 形状 | 实例 |
|------|------|------|
| 契约包 | 顶层基类 + 框架管件 + `impl/` 子层，三包对称 | `detection/`（Detector）、`temporal/`（Operator）、`offline/`（Segmenter） |
| 基础设施包 | 无基类、无 `impl/`，提供落盘/工具能力 | `feature/`（FeatureStore / FactLedger） |
| 活体包 | 由 manager 持有的 worker 池 | `visualization/` |

三类不可混谈——基础设施包与活体包本就不该有 `impl/`，不是「没做完」。

> **`cli.py` 例外条款**：服务包内允许有 `cli.py` 作为 `python -m` 离线/运维入口
> （`offline/cli.py`），但它是**单向出口**，不得被包内任何其他模块 import。

### `__init__.py` 两种形态，禁止中间态

- **门面型**（包内有活体）：docstring + `lifespan()` + 少量轻类型导出。
- **标记型**（无活体）：纯 docstring，不 re-export，消费方走深路径。
- **禁止中间态**：re-export 一大堆便利符号却无活体——唯一效果是把整棵子树的重依赖变 eager，
  收益仅是少打几个点。

**`__init__.py` 模块级只允许 import 轻量类型与 `contextlib`；一切指向 `instance` / `manager` /
`impl` 的 import 必须写在函数体内。** 这条是整个模式的开关：

```python
@asynccontextmanager
async def lifespan():
    from .instance import recording_service      # ← 写到文件顶部则前功尽弃
    recording_service.start()
    try:
        yield
    finally:
        recording_service.stop(timeout=10.0)
```

`storage/hls/__init__.py` 是**第三种**：facade，re-export 整个域的公开面让调用方分不出
`hls` 是包还是模块。代价是连带加载全部实现模块，所以那些模块的模块级必须保持
stdlib + `app.domain`。

### 单例只挂名，不干活

> **模块级单例的 `__init__` 只允许赋值和建空容器。任何「干活」——读配置文件、`importlib`
> 加载 impl、`mkdir`、连 DB、建线程池——一律推迟到 `start()` 或首次使用。**

`RecordingService` 是现成样板：`SerialTaskQueue` 在 `start()` 里建，不在构造函数
（队列是一次性的，放构造函数会让单例在 start/stop 两轮之后炸）。

三个文件三件事，互不重复：

| 文件 | 唯一职责 | 谁付代价 |
|------|---------|---------|
| `manager.py` / `service.py` | **类定义** | 想要类的人（含测试自己 new 一个带 mock 的） |
| `instance.py` | **那一个全局实例** | 只有明确要全局单例的人（router、lifespan body） |
| `__init__.py::lifespan()` | **起停编排** | 谁都不付（函数体延迟） |

单例写在 `manager.py` 末尾时，测试 `from ...manager import XManager` 想自造实例，**拿到类的
同时也把全局单例造出来了**。分出 `instance.py` 后这条路才干净。

> 一句话判据：**`import app.services.<svc>` 必须零副作用、零重依赖**；要活体就显式
> `from .instance import x`。

## 4. 单例引用面与依赖注入

**单例只允许被三类模块 import**：`run_control`（编排中枢）、`routers/*`（装配层）、单例自己
包内的 `lifespan()`。**服务与服务之间不得直接 import 对方单例。**

受管单例（门禁 `SINGLETONS` 表）：`stream_service` / `inference_manager` /
`persistence_manager` / `recording_service` / `health_monitor` / `run_controller`。

`client_manager` **不在此列**：它是零跨服务依赖的中台 leaf，谁都可以向下依赖它，限制它的引用
面没有意义。

两条**具名例外**（写在门禁的 `SINGLETON_EXCEPTIONS` 里，每条都有理由）：

- `health_monitor/manager.py`——它是与 `run_control` 并列的自动化协调者，按秒轮询各服务状态
  并发起重连/清理，天然要持多个协作者（`recording_service` 那个只用来在断流时登记一次残帧
  flush）。全部写在 `_resolve_deps()` 函数体内，其中 `run_controller` 那处是反向指回编排中枢
  做拆除。
- `inference/temporal/alarm_sink.py`——inference 产告警 → persistence 落库，跨服务但方向正确
  （下游依赖），sink 是这条方向唯一的窄接口。

依赖注入分三档：

| 对象 | 做法 |
|------|------|
| 活体单例 | **模块级单例，不注入**。测试不碰——真要碰说明该测的是里面的 seam |
| 有外部 I/O 的协作者（DB session、`LabelStudioClient`、`FeatureStore`、下游 service） | **构造注入 + 生产默认值**：`None` 时在 `start()` 里按 settings 建或取全局单例 |
| 纯方法模块 | **不注入**。要替换的是入参数据，不是依赖 |

三条禁令：不引入 DI 容器；`Depends()` 只用于请求级对象（DB session、当前 token），绝不注入
活体服务；不给单例加 `set_instance()` / `reset()` 后门。

## 5. 配置与静态资产

顶层 `config/` 的位置是刻意的：五份 `<svc>_config.yaml` 是运维要改的东西，不该埋进 Python
包；放顶层才能在部署时整目录覆盖或挂载。

**路径解析收敛到 `settings.config_dir`**（照 `settings.storage_base_dir` 的写法），`config.py`
不再各自 `Path(__file__).parent.parent.parent.parent` 数层级——数错一层就默默指错。

## 代码来源

- `app/domain/{frame,detection,alarm,render}.py`
- `app/storage/__init__.py`、`app/storage/_root.py`
- `app/services/utils/{__init__,media_timeline,vod_playlist}.py`
- `app/services/recording/{__init__,instance,service}.py`（门面型 + `instance.py` 样板）
- `app/services/persistence/__init__.py`（门面型）
- `app/services/inference/temporal/__init__.py`（标记型）
- `app/services/health_monitor/manager.py`（`_resolve_deps` 注入样板）
- `app/settings.py`（`storage_base_dir` / `config_dir`）
- `tests/test_import_hygiene.py`（`HEAVY` / `BUDGET` / `LAYER_PACKAGES` / `SINGLETONS`）
