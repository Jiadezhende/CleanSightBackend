> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Configuration Service

配置由三部分组成：`app/settings.py`（Pydantic settings，env 前缀 `CLEANSIGHT_`）、`config/*.yaml`（启动时只读）、
少量页面可改的运行时配置文件。各角色 `.env*` 取值、端口表与部署操作见 deploy skill 的
[runtime-config.md](../../.claude/skills/deploy/references/runtime-config.md)。

## fps / 时间配置分三层，衍生量永不进 yaml

| 层 | 放什么 | 判据 |
|----|--------|------|
| settings（`app/settings.py`） | 跨模块单一真源的真旋钮 + 时间概念 | 可自由调、不与某个产物强绑 |
| yaml（`config/*.yaml`） | 编排（选哪条 pipeline / 流）+ 契约（随产物钉死的量） | 配错会崩，或语义是「选择 / 契约」 |
| 衍生量（代码属性） | 由 settings 算出的换算结果 | 必须与真源一致，进 yaml 就成了第二真源 |

- **真旋钮只有两个**：`raw_fps = 30`（解码 CFR 帧率）、`inference_decimation = 2`（检测抽帧每 N 帧留 1）。
  检测率 = `raw_fps / inference_decimation`，整数因子只能命中整除率（30 → 15 / 10 / 7.5 / 6…）。
- **时间概念**以秒声明：`ca_maxlen_seconds = 30`、`ca_segment_seconds = 10`（HLS 段长由后者触发）。
- **衍生量**：`settings.inference_fps`（= 15.0）、`ClientConfig.ca_maxlen` / `ca_segment_len`（× `raw_fps` = 900 / 300 帧，
  经 `cq_kwargs()` 传给 CQ，`inference_decimation` 同路）、`DecoderConfig.default_fps`（= `raw_fps`，ffmpeg `fps=`）、
  `VisualizationWorkerPool.target_fps`（轮询率 = `raw_fps`）/ `output_fps`（= `inference_fps`）。
- **yaml 里唯一的 fps 是 `model_input_fps`**（`inference_config.yaml` 两处，均 7.5：`CleanOperator` 与 CLEAN 离线段）。
  它是模型契约，配错不崩、静默变差，故必填并在加载期校验：在线 `TemporalOperator.__init__` 要求 >0 且 ≤
  `settings.inference_fps`；离线 `CleanNodepGRUSegmenter` 构造要求 >0，入模时检测帧率低于它即 `ValueError`。
- **运行时从帧 ts 反推、不读任何配置**：HLS 段编码 fps（`app/storage/hls/_encode.py::effective_fps` = `(N-1)/span`，
  落在 [1, 60] 外或单帧 / span≤0 时回退 15.0）、WS 推帧率（`ai.py`）、模型入模密度
  （`app/services/inference/resample.py::resample_by_ts`）。

### yaml 写进未知字段：只有 recording 会当场崩

| loader | 未知字段（如误写 `raw_fps: 25`）的结果 |
|---|---|
| recording（`RecordingConfig.from_yaml` 末尾裸 `cls(**raw)`，不在 try 内） | `TypeError`，启动即崩 |
| client / stream / alarm / cleanup（`**dict` 构造包在 `try` 里） | 记一条 ERROR，**整份回退默认值**，进程照常起 |
| health_monitor（按键 `.get`）、inference（按键取段） | 忽略 |

其余 loader 靠 yaml 由 git 跟踪、部署整仓覆盖来保持干净。

## settings 其它关键项

- **`rtsp_read_timeout_s = 2.5`**：转成微秒喂 decoder ffmpeg 的 `-timeout`。存的是 flag 原值，实际断流判死延迟约为
  `2×` 本值（ffmpeg 行为，可能随版本变）。它与 health_monitor 的 `cleanup_timeout` 串联：判死占掉过多预算时
  `HealthMonitorWorker.start()` 只告警、不纠正。见 [SERVICE_STREAM.md](SERVICE_STREAM.md) / [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)。
- **`storage_base_dir` / `config_dir`**：相对路径一律以项目根为基解析，与进程 cwd 无关。存储根的唯一来源是
  `storage_base_dir`（env `CLEANSIGHT_STORAGE_DIR`），各方都读它、不互相灌值。七个服务 loader
  （`app/services/{client,inference,recording,stream,alarm}/config.py`、`app/daemons/{health_monitor,cleanup}/config.py`）
  一律 `settings.config_dir / "xxx.yaml"`；唯一例外是 `app/services/algorithm/colorstrip/config.py` 读同目录 `params.yaml`。
- **媒体 token**：`media_token_secret`（空则启动时随机生成）、`media_token_ttl = 300`。

## 环境文件与必填项

- `CLEANSIGHT_ENV` 选文件：`dev` → `.env.dev`（缺省）、`test` → `.env.test`、`prod` → `.env`。
- `_load_env_files()` 用 `os.environ.setdefault` 注入，**已存在的环境变量压过文件值**——启动脚本 export 的端口因此
  能压住 `.env*` 同名键。
- **必填六项**（`db_host` / `db_port` / `db_name` / `db_user` / `db_password` / `alarm_report_url`）没有默认值：
  缺任一项，`import app.settings` 即抛 pydantic `ValidationError`，与 `strict` 无关（`env_ignore_empty=True`，空值等同缺失）。
  `check_required_fields` 的 strict 分支（`strict=True` 且非 dev 才抛）实际只可能拦到 `CLEANSIGHT_DB_PORT=0`。

## 端口的唯一声明处是启动脚本

`start_backend.sh` 的 `BASE_*` / `start_backend.ps1` 的 `$Base*` 五行是端口唯一真源（两份脚本各自声明，改端口需同步）。
脚本导出 `CLEANSIGHT_PORT`、`CLEANSIGHT_MEDIAMTX_PROXY_PORT` / `_INTERNAL_PORT`、`GATEWAY_LISTEN_PORT` /
`GATEWAY_TARGET_PORT`、`MTX_RTSPADDRESS` / `MTX_RTPADDRESS` / `MTX_RTCPADDRESS`。`.env*` 不含端口项；`settings.py`
的 `port` / `mediamtx_*_port`、`mediamtx.yml`、网关 `config.ini` 里的端口只是脱离脚本单独跑时的回退值。
`CLEANSIGHT_PORT` 必须导出，否则 `python -m app.main` 读到的 `settings.port` 会与脚本不一致。端口表、NAT 1:1 约束见
[runtime-config.md](../../.claude/skills/deploy/references/runtime-config.md)。

## YAML 配置

| 文件 | 内容 |
|------|------|
| `config/inference_config.yaml` | 每个 stage 的 `detectors[]` / `rules[]`（Operator，含 `subscribes`、`params.window_seconds` / `model_input_fps`）/ `offline`，顶层 `batch_size`。权重路径写 `${CLEANSIGHT_MODEL_PATH:./app/data}/<文件>.pt`（`config.py::_expand_env_vars` 展开）。detector 导入或构造失败、operator 类导入失败、rule 缺 `class` / `subscribes` → 后端启动失败；operator 在每次 `start_workflow` 才 `cls(**kwargs)` 构造（`online/service.py`），参数错（如 `model_input_fps` 越界）只让该次 `/api/start` 回滚报错 |
| `config/stream_config.yaml` | `decoder:` 段：`default_width` / `default_height`、`pix_fmt`、`chunk_read_size`、`backpressure_ratio`；不含 fps |
| `config/client_config.yaml` | `frame:` 段 resize 宽高；stage 不在此（由 `start_run` 按 `current_step` 传给 CQ） |
| `config/recording_config.yaml` | `queue_size`、`sweep_interval_seconds`；没有 `workers`（段写恒单消费线程，配多了不报错、只让段间 tfdt 碰撞），不配任何时长或帧率 |
| `config/persistence_config.yaml` | 分段读：`storage:` → `daemons/cleanup/config.py::CleanupConfig`（`enable_cleanup` / `cleanup_days` / `cleanup_interval_seconds`）；`alarm:` → `services/alarm/config.py::AlarmServiceConfig`（`workers` / `queue_size`）。告警重试参数写死在 `alarm_worker.py` |
| `config/health_monitor_config.yaml` | `monitor:` 段：`check_interval` 1.0、`heartbeat_timeout` 5.0、`reconnect_interval` 5.0、`cleanup_timeout` 20.0、`orphan_timeout` 30.0、`task_max_duration` 1800.0 |
| `config/logging.json` | 日志 dictConfig，见下 |
| `app/services/algorithm/colorstrip/params.yaml` | 比色阈值、`max_image_bytes`、`default_profile`；随算法包走、不经 `config_dir`，现场调参只能改包内文件。见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md) |

### offline 段：非空即启用，`class` 必填，无兜底

- 每个 stage 的 `offline` 形状只有 `{class, params}`：空块 / 缺省 = 该 stage 不可跑离线；非空时 `class` 为全限定类路径、
  必须可导入，构造参数全部来自 `params`（`cls(**params)`），产出的 `TemporalSegment.producer` = 类名。
- 可跑校验只有 `InferenceConfig.require_offline(step_id)`：step 未定义或 `offline` 为空 → `ValidationError`。
  admin 提交（400）与 Runner 运行（CLI 退出码 1）共用。
- 在线进程从不实例化 `offline` 块的类：类路径错、权重缺失只在离线作业里暴露（该作业 failed）。
- 当前：step `"1"`（LEAK）`offline: {}`；step `"2"`（CLEAN）为 `CleanNodepGRUSegmenter`，`params` =
  `model_path: …/clean-offline-gru-nodep.pt`、`model_input_fps: 7.5`、`confidence_override: 1.0`、`min_duration_s: 0.2`。
  `model_input_fps` / `confidence_override` 须与训练口径一致，配错不报错（训练口径待核验）。
- yaml 注释给出三个备选类（只收 `model_path` / `min_duration_s`），换 `class` 须同时换 `params`：

| class | 权重 |
|---|---|
| `CleanNodepGRUSegmenter`（默认） | `clean-offline-gru-nodep.pt` |
| `CleanBiGRUSegmenter` | `clean-offline-bigru.pt` |
| `CleanASFormerSegmenter` | `clean-offline-asformer.pt` |
| `CleanMSTCNBiLSTMSegmenter` | `clean-offline-mstcn.pt` |

离线权重命名 `clean-offline-<模型>.pt`，与在线权重同放 `${CLEANSIGHT_MODEL_PATH:./app/data}`。

## Gateway 配置

FastAPI Gateway（`app/gateway.py`）的配置全在 settings：

- 开关与白名单：`gateway_enabled`、`gateway_allowed_ips`（逗号分隔，空 = 不限制）
- 普通档：`gateway_rate_limit` / `gateway_rate_window`，持续超限升级封禁 `gateway_rate_ban_threshold` / `gateway_rate_ban_window`
- 宽松档：`gateway_relaxed_prefixes`（默认 `/health,/task/message,/task/live,/task/history,/traceback,/admin-f3m8,/metrics`）+ `gateway_relaxed_rate_limit`
- 绕过档：`gateway_bypass_prefixes`（默认 `/media`）
- 反扫描：`gateway_scan_threshold` / `gateway_scan_window` / `gateway_ban_duration`

分档行为见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。MediaMTX 网关读 `GATEWAY_*` 环境变量或
`mediamtx_gateway/config.ini`。

## Lab 配置

- 静态 settings：`label_studio_token`（只在 env，页面不可见）、`label_studio_url` / `label_studio_default_project_id`
  （运行时配置的 env 回退值）、`lab_export_*`（临时目录、ffmpeg preset、单段 / 总时长 / 段数上限）。
- 运行时配置（`app/services/lab/runtime_config.py`，不是 `config.py`）：Label Studio URL、默认 project_id、任务列表
  数据源（`db` / `storage`）。持久化在 `{storage_base_dir}/lab_runtime_config.json`，文件存在用文件值、否则回退 env；
  改完即时生效、重启保留；读写经模块锁。

## 日志配置

- `config/logging.json` 由 uvicorn `--log-config` 加载（dictConfig），app 代码不调 `dictConfig`。路径是字面量，写在三处：
  `app/main.py` 的 `uvicorn.run`、`start_backend.sh`、`start_backend.ps1`。
- 内容：console handler（`colorlog.ColoredFormatter`）+ `file_info` / `file_warning` / `file_error` 三个
  `ConcurrentTimedRotatingFileHandler`（按级别分文件、按时间轮转）；root level `INFO`。
- `CLEANSIGHT_LOG_LEVEL`（`settings.log_level`，默认 `INFO`）在 `app/main.py` lifespan 开头设置 root logger 级别，
  覆盖 `logging.json` 的 root level。
- 日志编码规范见 [DEVELOPMENT.md §4](../DEVELOPMENT.md)。

## 单例何时读 yaml

`client_service` / `stream_service` / `cleanup_worker` 在 import 期、`recording_service` / `alarm_service` 在构造期读 yaml；
其余单例推迟到 `start()` 或首次使用。完整表见 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)
「单例构造」一节。

## 代码来源

- `app/settings.py`（真旋钮、`rtsp_read_timeout_s`、`storage_base_dir` / `config_dir`、`_load_env_files`、`check_required_fields`、gateway 前缀、Lab 项）
- `start_backend.sh` / `start_backend.ps1`（`BASE_*` / `$Base*`）、`.env.example`
- `app/main.py`（`log_level` 设 root 级别、`uvicorn.run(log_config=...)`）
- `app/services/{client,stream,recording,alarm,inference}/config.py`、`app/daemons/{cleanup,health_monitor}/config.py`
- `app/services/inference/stage_factory.py`（`create_offline_segmenter`）、`app/services/inference/online/temporal/operator.py`、`app/services/inference/offline/impl/clean.py`
- `app/storage/hls/_encode.py`（`effective_fps`）、`app/services/lab/runtime_config.py`、`app/services/algorithm/colorstrip/config.py`
- `config/*.yaml`、`config/logging.json`
