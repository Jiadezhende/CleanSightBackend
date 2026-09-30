> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Configuration Service

配置由 Pydantic settings、YAML 文件和少量运行时配置文件共同组成。

## fps/时间配置三层模型（关键不变式）

fps/时间相关配置归为**三层**，边界定死——这是防止"衍生量被手滑写回 yaml、与 settings 漂移"的核心约束。真源：`app/settings.py`、各服务 / daemon 的 `config.py` loader、`config/*.yaml`。

| 层 | 放什么 | 判据 | 铁律 |
|----|--------|------|------|
| **settings 级**（`app/settings.py`） | 跨模块单一真源的**真旋钮** + **时间概念** | 能自由调、调了行为变、不与另一产物强绑 | 整数 fps 旋钮只有 2 个（`raw_fps`/`inference_decimation`） |
| **yaml 级**（`config/*.yaml`） | **编排**（选哪条 pipeline/流）+ **契约**（随产物钉死的量） | 配错会崩（shape/key）或语义是"选择/契约" | 不含任何衍生量 |
| **衍生量**（代码属性） | settings 算出的换算结果 | 必须与真源严格一致、不能独立设 | **永不进 yaml**——进了就是第二真源 → 漂移 |

- **settings 真旋钮**：`raw_fps: int = 30`（生产者：解码 CFR 帧率）、`inference_decimation: int = 2`（采样器：检测抽帧"每 N 帧留 1"的唯一旋钮）；**时间概念**：`ca_maxlen_seconds: int = 30`、`ca_segment_seconds: int = 10`（缓存/段长以秒声明，非帧数）。检测率 = `raw_fps / inference_decimation`，整数因子故只命中 `raw_fps` 的整除率（30→15/10/7.5/6…，不支持 30→20 类非整除比）。
- **yaml 里的 fps 只有 `model_input_fps`**（`inference_config.yaml` 两处，均 7.5：CleanOperator `params` 与 CLEAN 离线段 `params`）——它是**模型契约**（随产物钉死、模型侧按 ts 重采样入模），配错不崩、静默降级，故必填 + 加载期校验（在线 `TemporalOperator.__init__` 校验 >0 且 ≤ `settings.inference_fps`；离线 `CleanNodepGRUSegmenter` 构造校验 >0，入模时输入帧率低于它即 `ValueError`）。
- **衍生量**（由 settings 算出、活在代码属性、永不进 yaml）：`settings.inference_fps`（property = `raw_fps/inference_decimation` = 15.0）、`ClientConfig.ca_maxlen`/`ca_segment_len`（`×raw_fps` = 900/300 帧）、`DecoderConfig.default_fps`（`= raw_fps`，ffmpeg `fps=` filter）、`VisualizationWorkerPool.target_fps`（轮询率 `= raw_fps`）/ `output_fps`（期望出帧率 `= inference_fps`）、`ClientQueues.inference_decimation`（直读 settings）。
- **未知字段的处理因 loader 而异**（谁往 yaml 误写衍生量，如 `raw_fps: 25`，结果不同）：
  - recording（`RecordingConfig.from_yaml` 末尾裸 `cls(**raw)`，不在 try 内）→ `TypeError` **当场崩**。
  - client / stream / alarm / cleanup 也是裸 `**dict` 构造，但包在 `from_yaml` 的 `try` 里 → 记 ERROR 后**该 loader 整份回退默认值**，进程照常起（静默用错配置，只有一条 ERROR 日志）。
  - health_monitor 按键 `.get` 取值、inference 按键取段 → 未知字段被**忽略**。
  - 因此「写错即崩」只对 recording 成立；其余靠 yaml 由 git 跟踪、部署整仓覆盖保持干净。
- **运行时反推**（既不在 settings 也不在 yaml，从帧 ts 现算）：HLS 段编码 `eff_fps` = `(N-1)/span`（**写侧真源是 `app/storage/hls/_encode.py` 的 `effective_fps`**，raw/processed 逐段各自反推；反推值落在 `[1,60]` 外或单帧/span≤0 时回退 15.0）、WS 推帧率（rendered 流实际到达率，`ai.py`）、模型入模密度（在线 / 离线共用 `app/services/inference/resample.py::resample_by_ts` 按 ts 降采样到 `model_input_fps`）。

## 其它 settings 关键项

- **`rtsp_read_timeout_s: float = 2.5`**（env `CLEANSIGHT_RTSP_READ_TIMEOUT_S`）：拉流 socket 读超时（秒），转成微秒喂 decoder ffmpeg 的 `-timeout`。**存 flag 原值，不存判死延迟**——实际断流判死延迟是 `2×` 本值（ffmpeg demux 层两次等待，实测关系、可能随 ffmpeg 版本变，折进配置值会让配置静默撒谎）。上界受 `health_monitor` 的 `cleanup_timeout` 约束，越界由 `HealthMonitorWorker.start()` 告警，见 [SERVICE_STREAM.md](SERVICE_STREAM.md) / [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)。
- **`storage_base_dir` / `config_dir` 两个路径 property 同款推导**：相对路径一律**以项目根为基**解析成绝对路径，进程 cwd 变了也不飘。`config_dir` = 项目根 `config/`，七个 `config.py`——`app/services/{client,inference,recording,stream,alarm}/config.py` + `app/daemons/{health_monitor,cleanup}/config.py`——一律 `settings.config_dir / "xxx.yaml"`，**不各写一遍 `Path(__file__).parent...` 数层级**（文件挪窝时会静默指错目录）。唯一例外是比色算法子包 `app/services/algorithm/colorstrip/config.py`：读同目录 `params.yaml`（见下「YAML 配置」）。

## 环境变量

`app/settings.py` 使用 `CLEANSIGHT_` 前缀。

环境文件加载规则：

- `CLEANSIGHT_ENV=dev`：加载 `.env.dev`
- `CLEANSIGHT_ENV=test`：加载 `.env.test`
- `CLEANSIGHT_ENV=prod`：加载 `.env`
- 默认是 dev

### 端口：启动脚本是唯一声明处

`start_backend.sh [dev|test|prod]`（Windows：`start_backend.ps1`）一条命令拉起整套（RTSP 网关含 MediaMTX + 后端 app），并按环境分配端口。

**端口的唯一声明处是启动脚本里的那五行基准值**（`.sh` 的 `BASE_*`、`.ps1` 的 `$Base*`），改端口只改那五行，其余全部派生并注入给三方进程：

| 端口 | 基准（dev/prod） | test（+2） |
|------|------------------|-----------|
| 后端 HTTP/WS | 8000 | 8002 |
| 网关对外 RTSP（客户端连这个） | 8004 | 8006 |
| MediaMTX RTSP 内部回源 | 18004 | 18006 |
| MediaMTX RTP / RTCP（UDP，内部） | 8002 / 8003 | 8004 / 8005 |

`dev`/`prod` 直接用基准值（二者同端口、分属不同机器故不冲突）；`test` 整体 `+2`，与同机 prod 隔离。

脚本据此导出 `CLEANSIGHT_PORT`（uvicorn 绑定口；**必须导出**，否则 `python -m app.main` 路径读 `settings.port` 会与脚本分叉）、`CLEANSIGHT_MEDIAMTX_PROXY_PORT`/`_INTERNAL_PORT`（后端回源改写）、`GATEWAY_LISTEN_PORT`/`GATEWAY_TARGET_PORT`（网关）、`MTX_RTSPADDRESS`/`MTX_RTPADDRESS`/`MTX_RTCPADDRESS`（MediaMTX 原生）。

**双真源已消除**：`.env` / `.env.dev` / `.env.test` 里**不再有任何端口项**（`.env.example` 保留的只是一段说明，指向启动脚本）。`settings.py` 的 `port` / `mediamtx_*_port`、`mediamtx.yml`、网关 `config.ini` 里的端口只是「脱离启动脚本单独跑某个进程」时的回退值。压制关系由 `_load_env_files()` 的 `setdefault` 保证：**已存在的环境变量 > 文件值**，脚本 export 的值压得过 `.env*` 同名键，故那五行是运行时的唯一真源（与网关侧「环境变量 > config.ini > 默认值」同向）。

两处工程约束：

- **两份脚本是两份独立声明**（`.sh` 与 `.ps1`），改端口要两边同步。
- **对外两个口（后端 HTTP、网关 RTSP）若经 NAT 映射，必须 1:1**：非等值映射会让 `stream/service.py:_rewrite_rtsp_url` 认不出本机 MediaMTX，后端绕公网回源、多数环境直接不通。
- `.ps1` 另有两件 Windows 特有的事：启动前查这三个 TCP 口是否已被占（占用即拒启并报出占用进程），以及退出时把它改过的端口环境变量**还原到进场快照**（PowerShell 会话里残留的旧值会让下次启动静默绑错口）。

严格模式：

- `strict=True` 且非 dev 时，缺少必需配置会阻止启动。
- dev 或非严格模式下，只打印警告。

必需配置包括数据库配置和外部接口 URL。

## YAML 配置

主要配置文件：

- `config/inference_config.yaml`：stage、detectors（流源）、rules（Operator，含 subscribes/window_seconds、CleanOperator 的 `model_input_fps` 模型契约）、offline（离线段，见下）、`batch_size`。**采样率/编码 fps 等衍生量与真旋钮（raw_fps/inference_decimation/ca_*_seconds）在 `app/settings.py`，不放此**（见上「三层模型」）。模型权重路径写成 `${CLEANSIGHT_MODEL_PATH:./app/data}/<文件名>.pt`（`config.py::_expand_env_vars` 展开）。**任一 detector / operator 配置错（类导入失败、构造抛错、rule 缺 `class` / `subscribes`）→ 后端启动失败**；没配 detector 的 stage 只是不生效，不报错。
- `config/stream_config.yaml`：FFmpeg 解码尺寸、pix_fmt、背压（`resize`/`backpressure` 等解码参数）。**不含 `default_fps`**（已删——解码 CFR 帧率由 `DecoderConfig.default_fps` 从 `settings.raw_fps` 派生）。
- `config/persistence_config.yaml`（文件名沿用，按段分读）：`storage:` 段 → `app/daemons/cleanup/config.py::CleanupConfig`（`enable_cleanup` / `cleanup_days` / `cleanup_interval_seconds`；扫描根委托 `settings.storage_base_dir`）；`alarm:` 段 → `app/services/alarm/config.py::AlarmServiceConfig`（`workers` / `queue_size`）。存储根不在此文件（在 settings）；告警上报重试参数写死在 `alarm_worker.py`，不在 yaml。
- `config/recording_config.yaml`：`queue_size`、`sweep_interval_seconds`。**没有 `workers`**（落盘的 `SerialTaskQueue` 恒单消费线程，多配不会报错、只会让段间 tfdt 碰撞），也**不配任何段时长/帧率**（段长由 `settings.ca_segment_seconds` 触发，段时长由写侧从帧 ts 反推）。
- `config/health_monitor_config.yaml`：`check_interval` 1.0、`heartbeat_timeout` 5.0、`reconnect_interval` 5.0、`cleanup_timeout` 20.0、`orphan_timeout` 30.0、`task_max_duration` 1800.0。其中 `cleanup_timeout` 与 `settings.rtsp_read_timeout_s` 串联（见上）。
- `config/client_config.yaml`：客户端帧尺寸、初始 stage 等。
- `config/logging.json`：日志 dictConfig（见下「日志配置」）。
- **例外：`app/services/algorithm/colorstrip/params.yaml`**——比色算法的全部阈值 + HTTP 入参上限 `max_image_bytes`（12000000）+ `default_profile`（`default`；另有未标定的 `warm_light` / `low_res` 示例档，base + profiles 两层深合并）。**刻意不在 `config/`、不经 `settings.config_dir`**：算法包零 `app.*` 依赖（连 `app.settings` 也不许 import），要能整个拷走单独跑；代价是现场调参要改包内文件，不能挂载覆盖。详见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)。

### offline 段 schema（离线分割）

每个 stage 下 `offline` 段（stage 粒度）绑定一个 `OfflineSegmenter`，形状只有 `{class, params}`：

- **空块 `{}` / 缺省 = 该 stage 不可跑离线**。
- **非空时 `class` 必填、须可导入**（全限定类路径，与在线 Detector/Operator 同风格，无短名注册表），否则 `create_offline_segmenter` fail-fast；构造参数全部来自 `params`（`cls(**params)`）。无 `name` / `subscribes` / `enabled` 字段；产出 `TemporalSegment.producer` = 类名。
- **可跑校验只有一条** `InferenceConfig.require_offline(step_id)`：step 未在 YAML 定义 → `ValidationError`（「未在推理配置中定义」）；`offline` 为空 → `ValidationError`（「未配置离线模型」）。作业服务提交时（admin 400）与 Runner 运行时（CLI 退出码 1）共用，**无兜底 stage**。
- 在线进程从不实例化 `offline` 块的类：类路径错、权重缺失只在离线运行时暴露（该作业 failed），不影响在线。

当前 YAML：step `"1"`（LEAK）`offline: {}`；step `"2"`（CLEAN）默认 `class: app.services.inference.offline.impl.clean.CleanNodepGRUSegmenter`，`params` = `model_path: …/clean-offline-gru-nodep.pt`、`model_input_fps: 7.5`、`confidence_override: 1.0`、`min_duration_s: 0.2`。`model_input_fps` / `confidence_override` 须与训练口径一致，**配错不报错、静默变差**（训练口径未经训练侧确认，待核验）。备选三类以注释给出，换 `class` 须同时换 `params`（这三类只收 `model_path` / `min_duration_s`）：

| class | 权重 |
|---|---|
| `CleanNodepGRUSegmenter`（默认） | `clean-offline-gru-nodep.pt` |
| `CleanBiGRUSegmenter` | `clean-offline-bigru.pt` |
| `CleanASFormerSegmenter` | `clean-offline-asformer.pt` |
| `CleanMSTCNBiLSTMSegmenter` | `clean-offline-mstcn.pt` |

离线权重命名约定 `clean-offline-<模型>.pt`，与在线权重同放 `${CLEANSIGHT_MODEL_PATH:./app/data}`；部署须放默认那份，缺失则 CLEAN 离线作业 failed（不影响在线）。来源：`app/services/inference/stage_factory.py` `create_offline_segmenter`、`app/services/inference/config.py` `require_offline`、`config/inference_config.yaml`。

## Gateway 配置

FastAPI Gateway（实现在 `app/gateway.py`，位于 app 根，与 `mediamtx_gateway` 进程共用 `IPWhitelistStore` / `RateLimitStore`）的配置在 settings 中：

- 开关与白名单：`gateway_enabled`、`gateway_allowed_ips`（逗号分隔，空=不限制）
- 普通配额：`gateway_rate_limit` / `gateway_rate_window`，超限升级封禁 `gateway_rate_ban_threshold` / `gateway_rate_ban_window`
- 三档路径策略（由松到紧反着看）：`gateway_bypass_prefixes`（完全跳过限流与反扫描，只给自带强鉴权的 `/media/*`）> `gateway_relaxed_prefixes` + `gateway_relaxed_rate_limit`（高频轮询接口独立高配额 bucket，不计入封禁升级与扫描检测）> 默认配额（未列入前两者的路径，含 `/ui-f3m8` 静态页与 `/algorithm`；分档细节见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)）
- 反扫描：`gateway_scan_threshold` / `gateway_scan_window` / `gateway_ban_duration`

MediaMTX Gateway 使用 `GATEWAY_*` 环境变量或 `mediamtx_gateway/config.ini`。

## Lab 配置

静态 settings：

- `label_studio_token`（只在 env，页面不可见、不可改）
- `label_studio_url` / `label_studio_default_project_id`（运行时配置的 env 回退值）
- `lab_export_*`（临时目录、ffmpeg preset、单段 / 总时长 / 段数上限）

运行时可持久化配置（`app/services/lab/runtime_config.py`，**不是** `config.py`：页面可改、落 JSON，与启动时只读的 `config/*.yaml` 是两回事）：

- Label Studio URL
- 默认 project_id
- 任务列表数据源（`db` / `storage`）

持久化在 `{storage_base_dir}/lab_runtime_config.json`；文件存在用文件值，否则回退 settings(env)；改完即时生效、重启保留；读写过模块锁（submit 在线程池多线程跑）。

## 日志配置

`start_backend.sh` 以 `uvicorn --log-config config/logging.json` 加载日志（`logging.config` dictConfig 格式），不在 app 代码里 `dictConfig`。`config/logging.json`：

- console handler：`colorlog.ColoredFormatter` 彩色输出。
- 文件 handler：`file_info` / `file_warning` / `file_error` 三个 `ConcurrentTimedRotatingFileHandler`，按级别分文件、时间轮转。
- root level `INFO`，handlers = console + 三个文件。

**路径硬编码、无环境变量开关**：三个调用点（`app/main.py` 的 `uvicorn.run`、`start_backend.sh`、`start_backend.ps1`）各写一次字面量 `config/logging.json`。曾有过 `settings.log_config`，但两个脚本从来是硬编码、根本不读它，属半个开关，已删。

日志**编码规范**（`[Module]` 前缀、`%` 惰性格式化、级别语义、热路径守卫）属贡献者约定，不在本库（见 docs/ 开发规范）。

> 注：无基于 `CLEANSIGHT_ENV` 的 dev/prod 日志级别分支，也未接 `LOG_LEVEL` 环境变量覆盖（旧文档的相关说法未落地）。

## 配置耦合点

- 真旋钮（`raw_fps`/`inference_decimation`）、时间概念（`ca_maxlen_seconds`/`ca_segment_seconds`）与 `storage_base_dir`/`config_dir` 均以 `app/settings.py` 为**单一真源**；cleanup / alarm / recording / client / inference / routers 都读 settings（或其派生属性），不反向钻进彼此的 YAML。
- HLS segment duration 由 `ca_segment_seconds`（→衍生 `ca_segment_len` 帧数）决定；段编码 fps 不再联动任何配置 fps，改由帧 ts 逐段反推（`hls._encode.effective_fps`）。
- 断流判死延迟（`2 × rtsp_read_timeout_s`，settings）与放弃重连时限（`cleanup_timeout`，health_monitor yaml）**串联**，分居两处但必须一起看；护栏只告警不纠正。
- trace/media token TTL 和 secret 由 settings 管理。

## 服务实例化与类型加载

单例一律「类在 `service.py`（daemon 为 `worker.py`）、那一个实例在 `instance.py`」，只有明确要单例的人才付构造代价。构造**不起线程、不连 DB、不 `importlib` 加载 impl**；但**何时读 yaml 不统一**，按代码现状分两档（与 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)「单例构造：不起线程，读配置视包而定」同一张表）：

| 单例 | 读 yaml 的时机 |
|---|---|
| `client_service`、`stream_service` | **import 期**：`client/service.py`、`stream/service.py` 模块级调 `get_*_config()`（stream 那处包在 try 里，失败退 None） |
| `recording_service`、`alarm_service` | **构造期**：`config=None` 即调 `get_recording_config()` / `get_alarm_config()` |
| `cleanup_worker` | **import 期**：`daemons/cleanup/instance.py` 模块级调 `get_cleanup_config()` 取构造参数 |
| `health_monitor_worker`、`inference_service`、`offline_job_service`、`run_control_service` | **构造零副作用**：`HealthMonitorWorker` 的 config 与四个协作者推迟到 `start()` 的 `_resolve_deps()`；`InferenceService` 的 stage 配置与重组件在 `start()` 建；`OfflineJobService` 的推理配置在提交时读 `load_stage_config()` 单例 |

后果：import 前五者的单例即读真实 `config/*.yaml` 并打加载日志（测试 import 路径同样会读）；`get_*_config()` / `load_stage_config()` 均为进程内缓存单例，只读一次。

类型 / 契约对象（`Detector` / `Operator` / `Frame` / `FrameDetection` / `DetectorOutput` 等，per-run / per-message）永不单例。

不变式：重资源（模型权重、worker 线程、per-run 组件）绝不在 import 或构造时创建，只在首次使用或显式 `.start()` 时；循环 import 用 point-of-use 惰性 import 打破。

## 代码来源

- `app/settings.py`（真旋钮、`rtsp_read_timeout_s`、`storage_base_dir`/`config_dir`、`_load_env_files` 的 setdefault 压制关系、Gateway 前缀、Lab 静态项）
- `start_backend.sh` / `start_backend.ps1`（端口唯一声明处：`BASE_*` / `$Base*` 五行）
- `.env.example`（端口为什么不在 `.env` 里的说明）
- `app/storage/hls/_encode.py`（`effective_fps` —— 段编码 fps 的运行时反推真源）
- `app/services/inference/config.py`（`require_offline`、`${VAR:default}` 展开、`load_stage_config` 缓存）
- `app/services/inference/stage_factory.py`（`create_offline_segmenter` offline schema；detector/operator 构造失败即抛）
- `app/services/{client,stream,recording,alarm}/config.py`
- `app/daemons/{cleanup,health_monitor}/config.py`、`app/daemons/cleanup/instance.py`
- `app/services/{client,stream}/service.py`（模块级读配置）、`app/services/{recording,alarm}/service.py`（构造读配置）
- `app/services/lab/runtime_config.py`
- `app/services/algorithm/colorstrip/{config.py,params.yaml}`
- `config/*.yaml`、`config/logging.json`

