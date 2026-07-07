> 更新时间：2026-07-07
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Configuration Service

配置由 Pydantic settings、YAML 文件和少量运行时配置文件共同组成。

## 环境变量

`app/settings.py` 使用 `CLEANSIGHT_` 前缀。

环境文件加载规则：

- `CLEANSIGHT_ENV=dev`：加载 `.env.dev`
- `CLEANSIGHT_ENV=test`：加载 `.env.test`
- `CLEANSIGHT_ENV=prod`：加载 `.env`
- 默认是 dev

严格模式：

- `strict=True` 且非 dev 时，缺少必需配置会阻止启动。
- dev 或非严格模式下，只打印警告。

必需配置包括数据库配置和外部接口 URL。

## YAML 配置

主要配置文件：

- `config/inference_config.yaml`：stage、detectors（流源）、rules（Operator，含 subscribes/window_seconds）、offline 占位、`batch_size`。**跨模块共享参数（raw_fps/inference_fps/ca_maxlen/ca_segment_len）已上浮 `app/settings.py` 单一真源，不再放此**。
- `config/inference_config_cpu.yaml`：CPU/mock 环境配置。
- `config/stream_config.yaml`：FFmpeg 解码尺寸、fps、pix_fmt、背压。
- `config/persistence_config.yaml`：HLS queue、alarm queue、存储目录、清理策略。
- `config/health_monitor_config.yaml`：心跳、重连、孤儿流、任务超时。
- `config/client_config.yaml`：客户端帧尺寸、初始 stage 等。

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

- 跨模块共享参数（raw_fps/inference_fps/ca_maxlen/ca_segment_len）与 `storage_base_dir` 均以 `app/settings.py` 为**单一真源**；persistence/client/inference/traceback 都读 settings，不反向钻进彼此的 YAML。
- HLS segment duration 与 `ca_segment_len`、raw/processed fps 有联动关系。
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
- `app/services/inference/config.py`
- `app/services/stream/config.py`
- `app/services/persistence/config.py`
- `app/services/health_monitor/config.py`
- `app/services/client/config.py`
- `app/services/lab/runtime_config.py`
- `config/*.yaml`

