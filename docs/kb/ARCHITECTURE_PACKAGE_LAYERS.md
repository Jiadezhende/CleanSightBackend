> 更新时间：2026-09-30
> 依据来源：代码分析（`app/` 全量 + `tests/test_import_hygiene.py`）
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 包分层与导入纪律

本文描述 `app/` 的分层现状、服务包内部形态，以及每条边界由哪个门禁用例守。导入写法等**规则正文**在
[DEVELOPMENT.md §8](../DEVELOPMENT.md)；落盘布局与存储层准入见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)。

## 1. 依赖单向向下，边界由 `test_import_hygiene` 锁死

```text
routers/ (+ routers/utils/)   装配层：HTTP 协议、token 签发、run 解析、DTO
   │  └──→ daemons/           只读其状态（routers/health.py 读 health_monitor_worker）
daemons/<name>/               按时钟自驱的后台任务（health_monitor / cleanup）；可依赖 services / storage
   │
services/<svc>/               run 或请求驱动的业务服务
   │
services/utils/               多个 service 共用、不属于任一个的能力；不得 import 兄弟 service 包
   ├──────────────────┐
   ▼                  ▼
storage/ (+ utils/)   db/      盘上产物 / 平台 DB（只读 ORM + query_*），两个平行 leaf、互不依赖
   │                  │
   ▼                  ▼
types/                         跨层契约 dataclass + exceptions（AppError 体系），整棵树的叶子
```

- `app/` 根只放组装与跨进程文件：`main.py`（lifespan 与路由装配）、`settings.py`（有 import 副作用）、
  `gateway.py`（后端中间件与 `mediamtx_gateway` 进程共用 `IPWhitelistStore` / `RateLimitStore`），加 `static/`、`data/`。
- 通用能力的落点：只一个包用留在该包；同层 ≥2 个包用进 `<层>/utils/`；跨层的契约与异常进 `types/`，其余放
  `app/` 根（`app/services/utils/__init__.py`）。
- **只有 routers 调 `app.db`**：全仓 `sqlalchemy` 只出现在 `app/db/`；routers 调 `db_tasks` / `db_alarms` 的
  `query_*`，不自开 session（`app/db/database.py::get_db` 零调用方）。services / daemons 都不 import `app.db`，
  但门禁并不禁止 services 调它；routers 也没有白名单门禁。各 router 的下层调用见
  [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)。

### 门禁映射

| 门禁用例 | 守什么 |
|---|---|
| `test_import_budget`（按 `BUDGET` 参数化） | 干净子进程 import 后不得出现预算外的 HEAVY、耗时低于上限；`FORBIDDEN_APP_IMPORTS` 另查 `offline.cli` 不拉起 `app.main` / `app.routers` / `inference.online` / `stream` |
| `test_layer_package_modules_are_all_budgeted` | 4 个白名单包里每个模块都有 `BUDGET` 条目 |
| `test_layer_package_imports_only_whitelisted_app_modules` | 4 个白名单包的 `app.*` 白名单 |
| `test_singleton_reference_surface` | 8 个受管单例的引用面（§4） |
| `test_services_do_not_import_routers` | services ↛ routers |
| `test_daemons_do_not_import_routers` | daemons ↛ routers |
| `test_services_do_not_import_daemons` | services ↛ daemons |
| `test_alarm_does_not_import_inference` | alarm ↛ inference（只许 inference → alarm） |
| `test_intra_package_relative_cross_package_absolute` | 包内相对 / 跨包绝对 / 相对不上翻；覆盖 `app/` 与 `mediamtx_gateway/*.py`，含函数体内 import |

按模块名判定的门禁都先经 `_abs_module()` 把相对导入还原成绝对名，写成相对绕不过去。

### 四个白名单包（`LAYER_PACKAGES`）

```text
app/storage            → app.storage, app.types, app.settings
app/db                 → app.db, app.types, app.settings
app/services/algorithm → app.services.algorithm                     （零 app.* 依赖，连 settings 都不许）
app/services/utils     → app.services.utils, app.storage, app.types, app.settings
```

- 用白名单而非黑名单：黑名单挡不住 `app.db` 这类不造环、不报错的依赖。`storage` 与 `db` 都不依赖对方，
  二者才能各自单独降级。
- `app/services/algorithm` 是自包含算法服务：零 `app.*` 依赖，整包可拷走单独跑，阈值与入参上限写在包内
  `params.yaml`（见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)）。
- 前缀检查是 `name == ok or name.startswith(ok + ".")`，所以 `app.services.utils` 不会放行 `app.services.lab`。
  若放行兄弟包，`services/utils` 就成了 service → service 依赖的后门，而单例门禁看不见这种转发。
- `services/utils` 成员：`vod_playlist` / `media_timeline`（只做断流判定，媒体轴在 `storage.hls`）/ `task_queue` /
  `worker_guard` / `pressure` / `metrics`；模块级状态只有 metrics 的 Prometheus 指标。

## 2. 重依赖只在三处出现，门禁只盯 torch / ultralytics / cv2

| 级 | 内容 | 模块顶层可否 import |
|----|------|------------------|
| L0 | stdlib、dataclass、`app.types` | 可以 |
| L1 | numpy、pydantic | 可以（`app.types` 已带 numpy） |
| L2 | torch / ultralytics / cv2 | 只能走下面三条通路 |
| L3 | 有副作用的：`app.db.database`（模块级 `create_engine`）、`app.settings`（读环境） | 被广泛 import 的模块顶层不行 |

L2 现存位置：

1. **`impl/` 顶层**：只经 `stage_factory._import_class` 按配置 `importlib` 加载（如
   `online/temporal/impl/clean.py` 顶层 `import torch`）。`impl/` 一旦被任何 `__init__.py` re-export 就退化为 eager。
2. **函数体内 import**：`storage/hls/_encode.py::write_mp4v`、`algorithm/colorstrip/grader.py` 的 cv2。
   另一种形态是在函数体内 import 整个重模块：`online/visualization/visualizer.py` 顶层 `import cv2`，
   由 `online/service.py` 在 `_build_components()` 里才 import `visualization_worker`。
3. **spawn 子进程 target 模块**：`online/detection/stage_worker.py` 顶层**不许** import torch，否则早于
   `run_stages` 钉 `CUDA_VISIBLE_DEVICES`（正确性约束）。

门禁 `HEAVY = ("torch", "ultralytics", "cv2")`；numpy / sqlalchemy 不在其中。

`BUDGET` 登记 `(模块, 允许的重依赖集合, 耗时上限秒)`。白名单包必须逐模块登记（标记型包根不加载域文件，登记包名
挡不住往域文件里塞 cv2）；其余登记的是各服务包根、`offline.cli`、`lab.service`、`recording.service`、daemons 与
`app.main`。⚠ `storage/hls/`、`storage/inference/` 是 facade，import 任一成员都会加载整个域，所以成员条目实测的是
整个 facade：注释里的「stdlib only」门禁验不到，只有 HEAVY 三项真正受守。

## 3. 服务包内部结构

```text
app/services/<svc>/
  __init__.py        见下「三种形态」
  service.py         活体类 <Svc>Service；无活体的包放模块级函数（lab/service.py、algorithm/service.py）
  instance.py        单例 <svc>_service 唯一定义处；无活体则无此文件
  config.py          启动时经 settings.config_dir 读本服务 yaml
  runtime_config.py  （lab 独有）页面可改、落 JSON 的运行时状态
  types.py           本包私有数据形状
  <what>_worker.py   线程 / 进程体：alarm_worker / sweep_worker / visualization_worker / stage_worker
  <noun>.py          无状态能力模块：reporter / clip_builder / label_studio_client / step_exporter / decoder / queues / naming / render …
  impl/              经 importlib 按配置加载的可插拔实现
  cli.py             python -m 入口，不被包内其他模块 import（inference/offline/cli.py、algorithm/colorstrip/cli.py）
app/daemons/<name>/  同上，入口换成 worker.py（<Name>Worker）+ instance.py（<name>_worker）
```

- 全仓没有 `workers/` / `strategies/` 子包、没有 `manager.py`；services / daemons 内没有下划线前缀模块
  （下划线只用于 `storage/hls/_*.py`、`storage/inference/_*.py` 这类 facade 私有实现）。
- `VisualizationWorkerPool`、`AlarmWorkerPool`、`SegmentSweeper` 类名沿用运维日志里的名字，文件名守骨架。
- 无活体服务（`lab`、`algorithm`）：标记型 `__init__` + `service.py` 模块级函数，无 `instance.py` / `lifespan()`，
  不进 `main.py` 启动序列。
- 骨架是现状形态，没有门禁强制文件角色。
- 文件名即依赖上界：`config.py` 只依赖 `app.settings` / `app.types` 与同包 `types.py`；`types.py` 只依赖
  stdlib / numpy / `app.types`（活体类型走 `TYPE_CHECKING`）。私有数据形状叫 `types.py`，ORM 行映射在
  `app/db/{tasks,alarms}.py`。

### `inference` 是唯一按链路分子包的服务

```text
app/services/inference/
  __init__.py                      lifespan()：inference_service 先起后停，offline_job_service 后起先停
  config.py / stage_factory.py / resample.py    online / offline 共享层
  online/     service(InferenceService) / instance(inference_service) / naming / types / render
              + detection/ temporal/（契约包） + visualization/（活体包）
  offline/    service(OfflineJobService) / instance(offline_job_service) / runner / segmenter / cli + impl/
```

online 与 offline 运行期互不 import，共用的只能放 `inference/` 这一层。

| 类别 | 形状 | 实例 |
|------|------|------|
| 契约包 | 顶层基类 + 框架管件 + `impl/` 子层 | `online/detection/`（Detector）、`online/temporal/`（Operator）、`offline/`（OfflineSegmenter） |
| 活体包 | `InferenceService` 持有的 worker 池，随 `start()/stop()` 起停，无 `impl/` | `online/visualization/` |

`offline/` 既是契约包，又持有离线作业服务的单例。产物落盘归 `app/storage/inference/`，不在 inference 包内。

### `__init__.py` 只有三种形态

- **门面型**（包内有活体）：docstring + `lifespan()`，零 re-export；模块级只 import `contextlib` / `logging`，
  指向 `instance` 的 import 写在 `lifespan()` 函数体内。实例：`services/{stream,alarm,recording,inference,run_control}`、
  `daemons/{cleanup,health_monitor}`（`run_control` 未声明 `__all__`，其余均为 `__all__ = ["lifespan"]`）。
- **标记型**（无活体）：纯 docstring，消费方走深路径。实例：`app/services`、`client`、`lab`、`algorithm`（及
  `colorstrip`）、`services/utils`、`inference/online`、`inference/offline` 及其子包、`app/types`、`app/db`、
  `app/daemons`、`app/routers/utils`、`app/storage`、`app/storage/utils`。
- **facade 型**（storage 重域子包）：`storage/hls/`、`storage/inference/` re-export 整个域的公开面，连带加载全部
  实现模块，所以这些模块的模块级只能是 stdlib + `app.types`（cv2 走函数体内）。

判据：`import app.services.<svc>` / `import app.daemons.<name>` 必须零重依赖，要活体就显式 `from .instance import x`。

### 单例构造不起线程；是否在 import / 构造期读 yaml 因包而异

模块级单例的构造不起线程、不连 DB、不 `importlib` 加载 impl；线程与一次性队列推迟到 `start()`
（例：`RecordingService` 的 `SerialTaskQueue` 在 `start()` 里建，放构造函数会让单例在两轮 start/stop 后炸）。

| 单例 | 读 yaml 的时机 |
|---|---|
| `client_service`、`stream_service` | import 期：`client/service.py`、`stream/service.py` 模块级调 `get_*_config()`（stream 那处包在 try 里，失败退 None） |
| `cleanup_worker` | import 期：`daemons/cleanup/instance.py` 模块级调 `get_cleanup_config()` |
| `recording_service`、`alarm_service` | 构造期：`config=None` 即调 `get_recording_config()` / `get_alarm_config()` |
| `health_monitor_worker`、`inference_service`、`offline_job_service`、`run_control_service` | 构造不读：`HealthMonitorWorker._resolve_deps()` 在 `start()` 取 config 与四个协作者；`InferenceService` 在 `start()` 读 stage 配置；`OfflineJobService` 提交时读 `load_stage_config()` |

所以 import 前五者即读真实 `config/*.yaml` 并打加载日志（测试 import 同样会读）；`get_*_config()` /
`load_stage_config()` 均为进程内缓存。类（`service.py` / `worker.py`）与单例（`instance.py`）分文件，测试 import 类
自造实例时不会顺带构造全局单例。

## 4. 单例引用面与依赖注入

受管单例（门禁 `SINGLETONS`，8 个）：

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

`client_service` 不在此列：它是零跨服务依赖的中台 leaf，谁都可以向下依赖。

单例只允许被三类文件 import（`_is_allowed_importer`）：`app/services/run_control/service.py`（编排中枢；
`instance.py` 不放行）、`app/routers/*`、任意 `*/__init__.py`（约定是本包 `lifespan()`）。现网引用：

- `run_control/service.py` 模块级取 inference / recording / stream（及 client）；
- `routers/api.py` 取 `run_control_service`，`routers/admin.py` 取 `offline_job_service`，`routers/health.py` 取 `health_monitor_worker`；
- 各门面型 `__init__.py` 的 `lifespan()` 取本包单例。

两条具名例外（`SINGLETON_EXCEPTIONS`）：

- `app/daemons/health_monitor/worker.py`：与 `run_control` 并列的自动化协调者。`_resolve_deps()` 函数体内取
  client / stream / inference / recording（recording 只用于断流时登记残帧 flush）；`cleanup_client()` 函数体内取
  `run_control_service` 做拆除。均不在模块级。
- `app/services/inference/online/temporal/alarm_sink.py`：模块级取 `alarm_service`，是 inference → alarm 方向唯一的窄接口。

依赖注入现状：活体单例不注入；有外部 I/O 的协作者走「构造注入 + 生产默认值」，`None` 时在构造或 `start()` 里取
（`RecordingService(clients=)`、`AlarmService(config=None)`、`HealthMonitorWorker` 由 `_resolve_deps()` 补）；纯方法模块
不注入。平台 DB 不注入 session，`query_*` 自开自关；lab 协作者（如 `LabelStudioClient`）在 `services/lab/service.py`
函数内按 settings 构造。没有 DI 容器，`Depends()` 零使用，单例没有 `set_instance()` / `reset()` 后门。

## 5. 导入写法

规则见 [DEVELOPMENT.md §8](../DEVELOPMENT.md)，由 `test_intra_package_relative_cross_package_absolute` 执行。

## 6. 配置目录与静态资产

- 运维可改的配置在仓库顶层 `config/`（六份 `<name>_config.yaml` + `logging.json`），经 `settings.config_dir`
  解析；唯一例外是 `app/services/algorithm/colorstrip/params.yaml`。细节见 [SERVICE_CONFIG.md](SERVICE_CONFIG.md)。
- 静态资产 `app/static/`（`admin/`、`lab/`、`vendor/`）以单一挂载 `/ui-f3m8` 提供，目录由 `__file__` 推导，
  见 [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)。

## 代码来源

- `tests/test_import_hygiene.py`（`HEAVY` / `BUDGET` / `FORBIDDEN_APP_IMPORTS` / `LAYER_PACKAGES` / `SINGLETONS` / `SINGLETON_EXCEPTIONS` / `_is_allowed_importer`）
- `app/services/utils/__init__.py`、`app/routers/utils/__init__.py`、`app/storage/{hls,inference}/__init__.py`（facade）
- `app/services/{stream,alarm,recording,inference,run_control}/__init__.py`、`app/daemons/{cleanup,health_monitor}/__init__.py`（门面型 `lifespan()`）
- `app/services/run_control/service.py`（模块级取单例）、`app/daemons/health_monitor/worker.py`（`_resolve_deps` / `cleanup_client`）
- `app/services/{client,stream}/service.py`、`app/daemons/cleanup/instance.py`（import 期读配置）；`app/services/{recording,alarm}/service.py`（构造期读配置）
- `app/services/inference/online/service.py`（`_build_components` 延迟 import 可视化）、`app/services/inference/online/detection/stage_worker.py`
- `app/db/database.py`、`app/main.py`、`app/settings.py`（`config_dir`）
