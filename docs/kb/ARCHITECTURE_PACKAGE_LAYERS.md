> 更新时间：2026-09-30
> 依据来源：代码分析（`app/` 全量 + `tests/test_import_hygiene.py`）
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 包分层与导入纪律

`app/` 下的子包依赖**单向向下**。本文回答两个问题：**哪一层能 import 哪一层**，以及
**一个服务包内部长什么样**。落盘布局与存储层准入判据不在这里，见
[DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)；导入写法的规范正文在 `docs/DEVELOPMENT.md` §8。

## 1. 层依赖图

```text
routers/ (+ routers/utils/)   装配层：HTTP 协议、token 签发、run 解析、URI 拼装、DTO
   │  └──→ daemons/           只读其状态（routers/health.py 读 health_monitor_worker）
daemons/<name>/               按时钟自驱的后台任务（health_monitor / cleanup）；可依赖 services / storage
   │
services/<svc>/               run 或请求驱动的业务服务
   │
services/utils/               服务层通用能力，多个 service 都要、但不属于任何一个
   │                          **不得 import 任何兄弟 service 包**
   ├──────────────────┐
   ▼                  ▼
storage/ (+ utils/)   db/      数据层（盘上产物） / 平台 DB（只读 ORM + query_*），两个平行 leaf、互不依赖
   │                  │
   ▼                  ▼
types/                         跨层契约：frame / detection / temporal / alarm / run 的 dataclass
                               + exceptions（AppError 体系）；零服务依赖，是整棵树的叶子
```

`app/` 根只放组装与跨进程文件：`main.py`（lifespan 与路由装配）、`settings.py`（谁都可以读，
但有副作用，故不在被广泛 import 的模块顶层）、`gateway.py`（后端中间件与 `mediamtx_gateway`
进程共用 `IPWhitelistStore` / `RateLimitStore`），加 `static/`、`data/`。

**通用能力按使用范围落点**：只一个包用 → 留那个包；同层 ≥2 个包用 → `<层>/utils/`
（`services/utils/`、`storage/utils/`、`routers/utils/`）；跨层 → 契约与异常进 `types/`，其余放
`app/` 根。依据 `app/services/utils/__init__.py`。

### routers 向下依赖 db / storage

routers 直接向下调 `app.db` 的查询函数（`db_tasks` / `db_alarms` 的 `query_*`）与 `app.storage`
的公开面，**不自开 DB session**：全仓 `sqlalchemy` 只出现在 `app/db/`，没有 router 用
`Depends(get_db)`（`app/db/database.py::get_db` 仍在、零调用方）。router 自己只留检查顺序、HTTP
错误映射与 DTO 组装，业务量交给 `query_*` 或 service 函数算；跨 router 共用的 HTTP 侧工具进
`app/routers/utils/`（`runs`：run 解析；`media_token`），准入是 ≥2 个 router 共用。这一条**没有门禁**
（`test_import_hygiene` 不给 routers 设白名单）。各 router 调哪些下层函数见
[ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)。

现状只有 routers 调 `app.db`；services、daemons 都不 import 它（门禁不禁止 services 调 `app.db`）。

### 门禁映射

边界由 `tests/test_import_hygiene.py` 锁死，不是靠自觉：

| 门禁用例 | 守什么 |
|---|---|
| `test_import_budget`（按 `BUDGET` 参数化） | 干净子进程 import 后不得出现预算外的 HEAVY、耗时低于上限；`FORBIDDEN_APP_IMPORTS` 另查 `offline.cli` 不拉起 `app.main` / `app.routers` / `inference.online` / `stream` |
| `test_layer_package_modules_are_all_budgeted` | 4 个白名单包里每个模块都有 `BUDGET` 条目 |
| `test_layer_package_imports_only_whitelisted_app_modules` | 4 个白名单包的 `app.*` 白名单 |
| `test_singleton_reference_surface` | 8 个受管单例的引用面（§4） |
| `test_services_do_not_import_routers` | services ↛ routers |
| `test_daemons_do_not_import_routers` | daemons ↛ routers |
| `test_services_do_not_import_daemons` | services ↛ daemons |
| `test_alarm_does_not_import_inference` | alarm ↛ inference（方向只许 inference → alarm） |
| `test_intra_package_relative_cross_package_absolute` | 包内相对 / 跨包绝对 / 相对不上翻（覆盖 `app/` 与 `mediamtx_gateway/*.py`，含函数体内 import） |

所有按模块名判定的门禁都先经 `_abs_module()` 把相对导入按文件位置还原成绝对名再判，**写成相对
绕不过去**。

### 四个白名单包（`LAYER_PACKAGES`）

```text
app/storage            → app.storage, app.types, app.settings
app/db                 → app.db, app.types, app.settings
app/services/algorithm → app.services.algorithm                     （零 app.* 依赖，连 settings 都不许）
app/services/utils     → app.services.utils, app.storage, app.types, app.settings
```

白名单而非黑名单：`app/storage` 能同时被写侧（recording）与读侧（lab / inference.offline /
routers）依赖的前提，是**它谁都不依赖**。黑名单只挡得住 `app.services.*`，挡不住 `app.db`——它进来
不造环、不报错，只会在某天想换存储时才发现。`app/db` 同理：不依赖 services / storage / routers，
DB 与 storage 才是可以各自单独降级的两个下游。`storage` 与 `services/utils` 的白名单都不含 `app.db`。

`app/services/algorithm` 不是分层 leaf，而是**自包含算法服务**：零 `app.*` 依赖是为了整个包能拷走、
单独跑，因此它的阈值与入参上限写在算法子包自己的配置文件里、不读 settings（见
[SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)）。

`app.services.utils` 这个前缀**不放行兄弟包**：检查是 `name == ok or name.startswith(ok + ".")`，
`app.services.lab` 差的正是那个点。破了它，本包就成了 service → service 依赖的后门
（lab 想调 recording 的东西，在这里加个转发函数就绕过去了，而单例门禁只盯单例、看不见转发）。
`services/utils` 不是无状态纯函数集合：成员是 `vod_playlist` / `media_timeline`（仅断流判定，媒体轴在
`storage.hls`）/ `task_queue` / `worker_guard` / `pressure` / `metrics`，模块级状态只有 metrics 的
Prometheus 指标。

## 2. 重依赖分级：L2 只有三条合法通路

| 级 | 内容 | 可否模块顶层 import |
|----|------|------------------|
| L0 | stdlib、dataclass、`app.types` | 任何地方 |
| L1 | numpy、pydantic | 任何地方（numpy 已在 `app.types` 里，躲不掉也不必躲） |
| L2 | **torch / ultralytics / cv2** | **禁止**，除非走下面三条通路 |
| L3 | 有副作用的：`app.db.database`（模块级 `create_engine` 建连接池）、`app.settings`（读环境） | 禁止在被广泛 import 的模块顶层 |

L2 的三条通路：

1. **`impl/` 下允许顶层 import**——只经 `stage_factory._import_class` 的 `importlib` 按配置
   加载，代价延迟支付。**代价条款：`impl/` 不得被任何 `__init__.py` re-export**，一旦
   re-export 立即退化为 eager。
2. **函数体内 import**——`impl/` 之外确需 cv2/torch 的地方。`app/storage/hls/_encode.py` 的
   cv2 在 `write_mp4v` 函数体内；`app/services/algorithm/colorstrip/grader.py` 的 cv2 同样在函数体内。
3. **spawn 子进程 target 的 `*_worker.py` 模块**——反而有更严的额外约束：
   `online/detection/stage_worker.py` 顶层不许 import torch，否则早于 `run_stages` 钉
   `CUDA_VISIBLE_DEVICES`，这是硬正确性约束而非性能偏好。

门禁只盯 L2（`HEAVY = ("torch", "ultralytics", "cv2")`）。numpy / sqlalchemy 刻意不在其中
——它们是 L1/L3、本来就到处都在用，放进去只会让每个模块都申报一次白名单，噪声大于信号。

### 导入预算逐模块登记

`BUDGET` 表登记 `(模块, 允许出现的重依赖集合, 耗时上限秒)`，**白名单包的每个模块逐个登记、不能
只登记包名**：包根是标记型 `__init__`、零 re-export，`import app.storage` 根本不加载任何域
文件，登记包名挡不住有人往域文件里塞 ffmpeg/cv2。新增模块必须同时加一行，由
`test_layer_package_modules_are_all_budgeted` 强制（只覆盖 4 个白名单包）。

其余登记：`app.types`、`app.services.lab.service`、`client` / `inference`（含 `online` / `offline` /
`offline.cli`）/ `alarm` / `recording`（+ `.service`）的包根、`app.daemons`（+ `cleanup`、
`health_monitor`、`health_monitor.instance`）、`app.main`。`app.db.*` 三个模块上限 1.0s（大头是
`sqlalchemy.orm`，不在 HEAVY）；`health_monitor` 两条守的是「import 包 / 单例不拉起任何 `app.services`」。

⚠ **子包成员条目实际量的是整个 facade，不是它自己**：`storage/hls/`、`storage/inference/` 的
`__init__` 是 facade，import 任何 `app.storage.hls.X` 都会先跑包 `__init__` 并连带加载全部实现模块。
所以 `_layout` / `_m3u8` / `_read` / `types` 几条的实测值与 `app.storage.hls` 一模一样。那里注释写的
「stdlib only」是**源码事实，不是门禁的结论**——往 `types.py` 塞一行 `import numpy` 不会让
任何一条红。真正被这些条目守住的只有 HEAVY 三项。

## 3. 服务包内部结构

```text
app/services/<svc>/
  __init__.py        纯 docstring；有活体时加 lifespan()。零 re-export，调用方一律走深路径
  service.py         入口：活体类 <Svc>Service；无活体的包放模块级函数（lab/service.py、algorithm/service.py）
  instance.py        单例 <svc>_service 唯一定义处；无活体则无此文件
  config.py          只读：启动时从 config/ 读本服务 yaml（经 settings.config_dir）
  runtime_config.py  （lab 独有）页面可改、落 JSON 的运行时状态——不是 config.py
  types.py           本包私有数据形状
  <what>_worker.py   线程 / 进程体，放包根：alarm_worker / sweep_worker / visualization_worker / stage_worker
  <noun>.py          无状态能力模块：reporter / clip_builder / label_studio_client / step_exporter / decoder / queues / naming / render
  impl/              经 importlib 按配置加载的可插拔实现
  cli.py             python -m 入口，单向出口
app/daemons/<name>/  同上，只把入口换成 worker.py（<Name>Worker）+ instance.py（<name>_worker）
```

- 全仓没有 `workers/` / `strategies/` 子包、没有 `manager.py`；services / daemons 内没有下划线前缀模块
  （`storage/hls/_*.py`、`storage/inference/_*.py` 这类 facade 私有实现模块用下划线，属 storage 约定）。
  单文件不建子包。
- 类名保留 `Pool` / `Sweeper` 的（`VisualizationWorkerPool`、`AlarmWorkerPool`、`SegmentSweeper`）
  不是骨架违规：文件名守骨架，类名是运维日志里在用的名字。
- 无活体服务（`services/lab`、`services/algorithm`）：标记型 `__init__` + `service.py` 放模块级函数，
  没有 `instance.py` / `lifespan()`，不进 `main.py` 启动序列。算法子包（`algorithm/colorstrip/`）自带
  `config.py` 读包内 `params.yaml`，依赖上界比常规更低：不依赖 `app.settings`。
- 骨架是**现状形态**，没有门禁强制文件角色；门禁只管导入（§1）。

文件名即依赖上界：`config.py` 只依赖 `app.settings` / `app.types` 与同包 `types.py`，里面出现
`from .service import ...` 就是错的；`types.py` 只依赖 stdlib / numpy / `app.types`（注解要的活体类型走
`TYPE_CHECKING`）。**私有数据形状叫 `types.py` 不叫 `models.py`**：与 `app/types/` 同义，都指数据形状；
ORM 行映射在 `app/db/{tasks,alarms}.py`（一张表一个模块）。

**`inference` 按链路分段**，是唯一有多子包的样板：

```text
app/services/inference/
  __init__.py                      lifespan()：inference_service 先起后停，offline_job_service 后起先停
  config.py / stage_factory.py / resample.py    online / offline 共享层
  online/     service(InferenceService) / instance(inference_service) / naming / types / render
              + detection/ temporal/（契约包） + visualization/（活体包）
  offline/    service(OfflineJobService) / instance(offline_job_service) / runner / segmenter / cli + impl/
```

online 与 offline 运行期互不 import，共用的只能放 `inference/` 这一层。子包角色两类：

| 类别 | 形状 | 实例 |
|------|------|------|
| 契约包 | 顶层基类 + 框架管件 + `impl/` 子层，三包对称 | `online/detection/`（Detector）、`online/temporal/`（Operator）、`offline/`（OfflineSegmenter） |
| 活体包 | 由 `InferenceService` 持有的 worker 池，起停跟着 `service.start()/stop()` | `online/visualization/` |

活体包本就不该有 `impl/`，不是「没做完」。`offline/` 是契约包，同时持有离线作业服务的活体单例
（`service.py` + `instance.py`）。产物落盘不在 inference 包内，归 `app/storage/inference/`。

> **`cli.py` 例外条款**：服务包内允许有 `cli.py` 作为 `python -m` 离线/运维入口
> （`inference/offline/cli.py`、`algorithm/colorstrip/cli.py`），但它是**单向出口**，不得被包内任何
> 其他模块 import。

### `__init__.py` 三种形态，禁止中间态

- **门面型**（包内有活体）：docstring + `lifespan()`，`__all__ = ["lifespan"]`，**零 re-export**。
  实例：`services/{stream,alarm,recording,inference}`、`daemons/{cleanup,health_monitor}`。
- **标记型**（无活体）：纯 docstring，消费方走深路径。实例：`app/services`、`client`、`run_control`、
  `lab`、`algorithm`（及 `colorstrip`）、`services/utils`、`inference/online`、`inference/offline` 及其
  子包、`app/types`、`app/db`、`app/daemons`、`app/routers/utils`、`app/storage`、`app/storage/utils`。
- **facade 型**（storage 重域子包）：`storage/hls/`、`storage/inference/` 的 `__init__` re-export 整个域的
  公开面，让调用方分不出是包还是模块。代价是连带加载全部实现模块，所以那些模块的模块级必须保持
  stdlib + `app.types`（cv2 走函数体内）。
- **禁止中间态**：re-export 一大堆便利符号却无活体——唯一效果是把整棵子树的重依赖变 eager，
  收益仅是少打几个点。

**`__init__.py` 模块级只允许 `contextlib` / `logging`；一切指向 `instance` / `service` / `impl` 的
import 必须写在函数体内。** 这条是整个模式的开关：

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

> 一句话判据：**`import app.services.<svc>` / `import app.daemons.<name>` 必须零重依赖**；要活体就显式
> `from .instance import x`。

### 单例构造：不起线程，读配置视包而定

模块级单例的构造**不起线程、不连 DB、不 `importlib` 加载 impl**——线程与一次性队列一律推迟到
`start()`。`RecordingService` 是样板：`SerialTaskQueue` 在 `start()` 里建，不在构造函数（队列是
一次性的，放构造函数会让单例在 start/stop 两轮之后炸）。

读配置文件则不统一，现状如下：

| 单例 | 构造 / import 期读 yaml？ |
|---|---|
| `health_monitor_worker`、`inference_service`、`offline_job_service`、`run_control_service` | 否：配置与协作者在 `start()` 或首次使用时现取（`HealthMonitorWorker._resolve_deps()`） |
| `client_service`、`stream_service` | 是：`client/service.py`、`stream/service.py` 模块级调 `get_*_config()` |
| `recording_service`、`alarm_service` | 是：构造时 `config=None` 即调 `get_recording_config()` / `get_alarm_config()` |
| `cleanup_worker` | 是：`daemons/cleanup/instance.py` 模块级调 `get_cleanup_config()` 取构造参数 |

因此 import 后五者的单例会读真实 `config/*.yaml` 并打加载日志；`get_*_config()` 均为进程内缓存单例。

三个文件三件事，互不重复：

| 文件 | 唯一职责 | 谁付代价 |
|------|---------|---------|
| `service.py`（daemon 为 `worker.py`） | **类定义** | 想要类的人（含测试自己 new 一个带替身的） |
| `instance.py` | **那一个全局实例** | 只有明确要全局单例的人（router、lifespan body、编排中枢） |
| `__init__.py::lifespan()` | **起停编排** | 谁都不付（函数体延迟） |

类与单例分文件后，测试 `from ...service import XService` 自造实例时不会顺带造出全局单例。

## 4. 单例引用面与依赖注入

受管单例（门禁 `SINGLETONS` 表，8 个）：

```text
stream_service          app.services.stream.instance
inference_service       app.services.inference.online.instance
offline_job_service     app.services.inference.offline.instance
alarm_service           app.services.alarm.instance
cleanup_worker          app.daemons.cleanup.instance
recording_service       app.services.recording.instance
health_monitor_worker   app.daemons.health_monitor.instance
run_control_service     app.services.run_control.instance
```

`client_service` **不在此列**：它是零跨服务依赖的中台 leaf，谁都可以向下依赖它，限制它的引用
面没有意义。

**单例只允许被三类文件 import**（`_is_allowed_importer`）：`app/services/run_control/service.py`
（编排中枢；`instance.py` 不放行）、`app/routers/*`（装配层）、`*/__init__.py`（约定是本包的
`lifespan()`；门禁实际放行任意 `__init__.py`）。**服务与服务之间不得直接 import 对方单例。**
现网引用：`run_control/service.py` 模块级取 client / inference / recording / stream；`routers/api.py`
取 `run_control_service`；`routers/admin.py` 取 `offline_job_service`；`routers/health.py` 取
`health_monitor_worker`。

两条**具名例外**（写在门禁的 `SINGLETON_EXCEPTIONS` 里，每条都有理由）：

- `app/daemons/health_monitor/worker.py`——与 `run_control` 并列的自动化协调者，按秒轮询各服务状态
  并发起重连/清理，天然要持多个协作者：`_resolve_deps()` 函数体内取 client / stream / inference /
  recording 四个（`recording_service` 只用来在断流时登记一次残帧 flush）；`cleanup_client()` 函数体内
  另取 `run_control_service`，反向指回编排中枢做拆除。均不在模块级。
- `app/services/inference/online/temporal/alarm_sink.py`——模块级取 `alarm_service`：inference 产告警 →
  alarm 上报，跨服务但方向正确（下游依赖），sink 是这条方向唯一的窄接口。

依赖注入分三档：

| 对象 | 做法 |
|------|------|
| 活体单例 | **模块级单例，不注入**。测试不碰——真要碰说明该测的是里面的 seam |
| 有外部 I/O 的协作者（下游 service、配置） | **构造注入 + 生产默认值**：`None` 时在构造或 `start()` 里按 settings 建或取全局单例。例：`RecordingService(clients=)`、`HealthMonitorWorker` 五参缺省由 `_resolve_deps()` 现取、`AlarmService(config=None)` |
| 纯方法模块 | **不注入**。要替换的是入参数据，不是依赖 |

平台 DB 不注入 session：调用方调 `app.db.*.query_*`，函数自己开关 session。`LabelStudioClient` 等
lab 协作者由 `services/lab/service.py` 在函数内按 settings 构造。

三条禁令：不引入 DI 容器；`Depends()` 不注入活体服务（现全仓零使用，DB 访问走查询函数）；不给单例加
`set_instance()` / `reset()` 后门。

## 5. 导入写法

包内一律相对（`from .x` / `from .sub.x`）、跨包一律绝对（`from app.…`）、相对不上翻（level > 1
禁止）。判据是「目标是否本包后代」，于是 `from app.` 开头 ⇔ 外部依赖。这不只是风格：单例引用面、
分层白名单都按模块名判定，写法统一才不会有第二种表达同一依赖的方式。由
`test_intra_package_relative_cross_package_absolute` 执行，规范正文见 `docs/DEVELOPMENT.md` §8。

## 6. 配置与静态资产

顶层 `config/` 的位置是刻意的：六份 yaml（`client` / `health_monitor` / `inference` / `persistence` /
`recording` / `stream`，文件名 `<name>_config.yaml`）加 `logging.json` 是运维要改的东西，不该埋进 Python
包；放顶层才能在部署时整目录覆盖或挂载。`persistence_config.yaml` 由两方分段读：`storage:` 段归
`daemons/cleanup/config.py`，`alarm:` 段归 `services/alarm/config.py`。唯一例外是
`app/services/algorithm/colorstrip/params.yaml`：算法包零 `app.*` 依赖，配置随包走、不经 `config/`。

**路径解析收敛到 `settings.config_dir`**（照 `settings.storage_base_dir` 的写法），`config.py`
不各自 `Path(__file__).parent.parent.parent.parent` 数层级——数错一层就默默指错。

静态资产 `app/static/`（`admin/`、`lab/`、`vendor/`）由 `main.py` 以单一挂载 `/ui-f3m8` 提供，目录由
`__file__` 推导、不依赖 CWD。

## 代码来源

- `app/types/{frame,detection,temporal,alarm,run,exceptions}.py`
- `app/db/__init__.py`、`app/db/database.py`
- `app/storage/__init__.py`、`app/storage/utils/{root,fs}.py`、`app/storage/{hls,inference}/__init__.py`（facade）
- `app/services/utils/__init__.py`（边界与成员）
- `app/services/recording/{__init__,instance,service}.py`（门面型 + `instance.py` 样板）
- `app/services/alarm/{__init__,service,instance}.py`（门面型；构造读配置）
- `app/services/inference/__init__.py`（online / offline 分段、`lifespan()`）
- `app/services/algorithm/__init__.py`（无活体、零 `app.*` 依赖）
- `app/daemons/__init__.py`、`app/daemons/cleanup/{__init__,instance}.py`
- `app/daemons/health_monitor/worker.py`（`_resolve_deps` / `cleanup_client`）
- `app/routers/utils/__init__.py`
- `app/main.py`（lifespan 嵌套、`/ui-f3m8` 挂载）
- `app/settings.py`（`storage_base_dir` / `config_dir`）
- `tests/test_import_hygiene.py`（`HEAVY` / `BUDGET` / `FORBIDDEN_APP_IMPORTS` / `LAYER_PACKAGES` / `SINGLETONS` / `SINGLETON_EXCEPTIONS`）
