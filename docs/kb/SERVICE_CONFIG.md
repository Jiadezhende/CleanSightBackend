> 更新时间：2026-05-24
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

- `config/inference_config.yaml`：stage、模型、Analyzer、全局 fps、batch、队列长度。
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

## 配置耦合点

- persistence 和 client 会读取 inference config 中的 fps、队列长度，保证全局一致。
- HLS segment duration 与 `ca_segment_len`、raw/processed fps 有联动关系。
- trace/media token TTL 和 secret 由 settings 管理。

## 代码来源

- `app/settings.py`
- `app/services/inference/config.py`
- `app/services/stream/config.py`
- `app/services/persistence/config.py`
- `app/services/health_monitor/config.py`
- `app/services/client/config.py`
- `app/services/lab/runtime_config.py`
- `config/*.yaml`

