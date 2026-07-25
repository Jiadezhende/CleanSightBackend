> 更新时间：2026-07-25
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Configuration Service

配置由 Pydantic settings、YAML 文件和少量运行时配置文件共同组成。

## fps/时间配置三层模型（关键不变式）

fps/时间相关配置归为**三层**，边界定死——这是防止"衍生量被手滑写回 yaml、与 settings 漂移"的核心约束。真源：`app/settings.py`、四个 config loader、`config/*.yaml`。

| 层 | 放什么 | 判据 | 铁律 |
|----|--------|------|------|
| **settings 级**（`app/settings.py`） | 跨模块单一真源的**真旋钮** + **时间概念** | 能自由调、调了行为变、不与另一产物强绑 | 整数 fps 旋钮只有 2 个（`raw_fps`/`inference_decimation`） |
| **yaml 级**（`config/*.yaml`） | **编排**（选哪条 pipeline/流）+ **契约**（随产物钉死的量） | 配错会崩（shape/key）或语义是"选择/契约" | 不含任何衍生量 |
| **衍生量**（代码属性） | settings 算出的换算结果 | 必须与真源严格一致、不能独立设 | **永不进 yaml**——进了就是第二真源 → 漂移 |

- **settings 真旋钮**：`raw_fps: int = 30`（生产者：解码 CFR 帧率）、`inference_decimation: int = 2`（采样器：检测抽帧"每 N 帧留 1"的唯一旋钮）；**时间概念**：`ca_maxlen_seconds: int = 90`、`ca_segment_seconds: int = 10`（缓存/段长以秒声明，非帧数）。检测率 = `raw_fps / inference_decimation`，整数因子故只命中 `raw_fps` 的整除率（30→15/10/7.5/6…，不支持 30→20 类非整除比）。
- **yaml 唯一的 fps 是 `model_input_fps: 7.5`**（`inference_config.yaml` CleanOperator `params`）——它是**模型契约**（随产物钉死、模型侧按 ts 重采样入模），配错不崩、静默降级，故必填 + 加载期校验（`TemporalOperator.__init__` 对 `None`/`≤0` 暴露信号）。
- **衍生量**（由 settings 算出、活在代码属性、永不进 yaml）：`settings.inference_fps`（property = `raw_fps/inference_decimation` = 15.0，viz 轮询率）、`ClientConfig.ca_maxlen`/`ca_segment_len`（`×raw_fps` = 2700/300 帧）、`DecoderConfig.default_fps`（`= raw_fps`，ffmpeg `fps=` filter）、`VizWorkerPool.target_fps`（`= inference_fps`）、`ClientQueues.inference_decimation`（直读 settings）。
- **天然护栏**：四个 config loader 都是裸 `**dict`、不做字段过滤——谁往 yaml 误写衍生量（如 `raw_fps: 25`），构造即 `TypeError` **当场崩**，无需额外校验。
- **运行时反推**（既不在 settings 也不在 yaml，从帧 ts 现算）：HLS 段编码 `eff_fps` = `(N-1)/span`（`hls_strategy._effective_fps`，raw/processed 逐段各自反推）、WS 推帧率（rendered 流实际到达率，`ai.py`）、模型入模密度（`_resample_by_ts` 重采样到 `model_input_fps`）。

## 环境变量

`app/settings.py` 使用 `CLEANSIGHT_` 前缀。

环境文件加载规则：

- `CLEANSIGHT_ENV=dev`：加载 `.env.dev`
- `CLEANSIGHT_ENV=test`：加载 `.env.test`
- `CLEANSIGHT_ENV=prod`：加载 `.env`
- 默认是 dev

### 环境端口隔离

`start_backend.sh [dev|test|prod]` 一条命令拉起整套（RTSP 网关含 MediaMTX + 后端 app），并按环境分配端口。基准端口（`dev`/`prod` 直接用，二者同端口、分属不同机器故不冲突）与 `test` 偏移（整体 +100，与同机 prod 隔离）：

| 端口 | 基准（dev/prod） | test（+100） |
|------|------------------|--------------|
| 后端 HTTP/WS（`BACKEND_PORT`） | 8000 | 8100 |
| 网关对外 RTSP（`PROXY_PORT`，客户端连这个） | 8004 | 8104 |
| MediaMTX RTSP 内部回源（`INTERNAL_PORT`） | 18004 | 18104 |
| MediaMTX RTP/RTCP（UDP，内部） | 8002 / 8003 | 8102 / 8103 |

脚本据此导出 `CLEANSIGHT_MEDIAMTX_PROXY_PORT`/`_INTERNAL_PORT`（后端回源改写）、`GATEWAY_LISTEN_PORT`/`GATEWAY_TARGET_PORT`（网关）、`MTX_RTSPADDRESS`/`MTX_RTPADDRESS`/`MTX_RTCPADDRESS`（MediaMTX 原生）。来源：`start_backend.sh`。

严格模式：

- `strict=True` 且非 dev 时，缺少必需配置会阻止启动。
- dev 或非严格模式下，只打印警告。

必需配置包括数据库配置和外部接口 URL。

## YAML 配置

主要配置文件：

- `config/inference_config.yaml`：stage、detectors（流源）、rules（Operator，含 subscribes/window_seconds、CleanOperator 的 `model_input_fps` 模型契约）、offline（离线段，见下）、`batch_size`。**采样率/编码 fps 等衍生量与真旋钮（raw_fps/inference_decimation/ca_*_seconds）在 `app/settings.py`，不放此**（见上「三层模型」）。
- `config/inference_config_cpu.yaml`：CPU/mock 环境配置。
- `config/stream_config.yaml`：FFmpeg 解码尺寸、pix_fmt、背压（`resize`/`backpressure` 等解码参数）。**不含 `default_fps`**（已删——解码 CFR 帧率由 `DecoderConfig.default_fps` 从 `settings.raw_fps` 派生）。
- `config/persistence_config.yaml`：HLS queue、alarm queue、存储目录、清理策略。
- `config/health_monitor_config.yaml`：心跳、重连、孤儿流、任务超时。
- `config/client_config.yaml`：客户端帧尺寸、初始 stage 等。

### offline 段 schema（离线分割）

每个 stage 下 `offline` 段（stage 粒度）路由到一个 `OfflineSegmenter`。启用判据是 **presence 驱动**——不再有 `enabled` 布尔开关：

- **空块 `{}` / 缺省 = 不启用**（`create_offline_segmenter` 返回 None，Runner skip）。这是临时禁用某 stage offline 的唯一方式（留空或整段删除/注释）。
- **非空即视为有意启用**；此时字段 `name` / `subscribes` / `class` 必填，缺任一 **fail-fast 抛 `ValueError`**（旧的「配全字段却漏写 `enabled: true` 导致静默不跑」的降级已消除）。`subscribes` 必须全命中同 stage 的 detector；`params` 不得重复声明 `name`/`subscribes`；`class` 为全限定类路径（与在线 Detector/Operator 同风格，无短名注册表）。
- `resolve_stage(step_id)`：数字 step 命中即恒等，**未知 step_id 回退 `MOCK` 并打 WARN**（与在线 `InferenceManager.resolve_stage` 对齐同源同义——两链路兜底都可见，避免「-1 冒烟」与「真打错 step」混淆）。

当前 YAML 现状：生产 stage（bubble/bending=step1、clean=step2）`offline` 均保持 `{}` 不启用（守 CLAUDE.md 硬规矩，离线不触碰在线 B2B 测试）；仅 **MOCK stage 的 `offline` 启用**（`class: ...segmenters.mock.BrushRulesSegmenter`, `subscribes: [mock]`），作「能端到端跑的配置化路由样例」。CLEAN 真实离线模型（`segmenters.clean` 的 MS-TCN/ASFormer/BiGRU 系列）以注释形式示例，开发期手动跑需在 dev 配置里临时把 `CLEAN.offline` 配上 `name/subscribes/class`。来源：`app/services/inference/stage_factory.py` `create_offline_segmenter`、`app/services/inference/config.py` `resolve_stage`、`config/inference_config.yaml`。

## Gateway 配置

FastAPI Gateway 配置在 settings 中：

- `gateway_enabled`
- `gateway_allowed_ips`
- `gateway_rate_limit`
- `gateway_relaxed_prefixes`
- `gateway_bypass_prefixes`
- `gateway_scan_threshold`
- `gateway_ban_duration`

MediaMTX Gateway 使用 `GATEWAY_*` 环境变量或 `mediamtx_gateway/config.ini`。

## Lab 配置

静态 settings：

- `label_studio_token`
- `lab_export_*`

运行时可持久化配置：

- Label Studio URL
- 默认 project_id

来源：`app/services/lab/runtime_config.py`

## 日志配置

`start_backend.sh` 以 `uvicorn --log-config logging_config.json` 加载日志（`logging.config` dictConfig 格式），不在 app 代码里 `dictConfig`。`logging_config.json`：

- console handler：`colorlog.ColoredFormatter` 彩色输出。
- 文件 handler：`file_info` / `file_warning` / `file_error` 三个 `ConcurrentTimedRotatingFileHandler`，按级别分文件、时间轮转。
- root level `INFO`，handlers = console + 三个文件。
- `logging_config_fallback.json` 为兜底配置。

日志**编码规范**（`[Module]` 前缀、`%` 惰性格式化、级别语义、热路径守卫）属贡献者约定，不在本库（见 docs/ 开发规范）。

> 注：无基于 `CLEANSIGHT_ENV` 的 dev/prod 日志级别分支，也未接 `LOG_LEVEL` 环境变量覆盖（旧文档的相关说法未落地）。

## 配置耦合点

- 真旋钮（`raw_fps`/`inference_decimation`）、时间概念（`ca_maxlen_seconds`/`ca_segment_seconds`）与 `storage_base_dir` 均以 `app/settings.py` 为**单一真源**；persistence/client/inference/traceback 都读 settings（或其派生属性），不反向钻进彼此的 YAML。
- HLS segment duration 由 `ca_segment_seconds`（→衍生 `ca_segment_len` 帧数）决定；段编码 fps 不再联动任何配置 fps，改由帧 ts 逐段反推（`_effective_fps`）。
- trace/media token TTL 和 secret 由 settings 管理。

## 服务实例化与类型加载

对象按「是否单例 + 何时构造」分四类（详见 `docs/update/20260701_SERVICE_INSTANTIATION_DESIGN.md`，部分为设计草案、**待核验**）：

- **A 饿汉单例（import 时）**：`client_manager`、`stream_service`、`persistence_manager`、`run_controller`——构造廉价、无重资源。
- **B leaf-lazy 单例（消费方显式 import）**：`inference_manager`（`instance.py`）——读 `inference_config.yaml` fail-fast，但 YOLO 权重/worker 线程等重资源仍惰性，不在 import/构造时加载。
- **C DI 装配（assembler 点）**：`GlobalHealthMonitor` 在 `routers/health.py` 注入依赖构造。
- **D 类型/契约（per-run/message）**：`Detector`/`Operator`/`Frame`/`FrameDetections` 等，永不单例。

不变式：重资源（模型权重、worker 线程、per-run 组件）绝不在 import 或构造时创建，只在首次使用或显式 `.start()` 时；循环 import 用 point-of-use 惰性 import 打破。

## 代码来源

- `app/settings.py`
- `start_backend.sh`（环境端口隔离）
- `app/services/inference/config.py`（`resolve_stage`）
- `app/services/inference/stage_factory.py`（`create_offline_segmenter` offline schema）
- `app/services/stream/config.py`
- `app/services/persistence/config.py`
- `app/services/health_monitor/config.py`
- `app/services/client/config.py`
- `app/services/lab/runtime_config.py`
- `config/*.yaml`

