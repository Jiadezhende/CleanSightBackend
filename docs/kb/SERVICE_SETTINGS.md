> 更新时间：2026-11-20
> 依据来源：app/settings.py、配置模板、网关及 Lab 实现、运行配置回归测试
> 可信级别：以当前仓库代码、配置、测试为准

# Settings 运行配置完整说明

每个部署目录使用一份 `config/settings.yaml`，从 [模板](../../config/settings.example.yaml) 创建。模板只展示常规部署项；未填写的可选字段沿用 [Settings](../../app/settings.py) 默认值。本文覆盖当前全部 48 个可配置字段，分为常规部署和高级覆盖；后者按需加入同一文件，无需新建配置文件。字段及默认值以代码为准。

## 加载、覆盖与生效时机

配置链为 `get_settings()` → `read_runtime_config()` → `load_runtime_environment()` → Pydantic `Settings`。首次访问共享 settings 时实例化，后续复用同一实例；只导入 schema／解析函数不要求运行凭据。

- 文件路径固定为项目根下的 `config/settings.yaml`，不按 dev／prod／test 选择文件，也不读取 `.env*`。文件不存在时可全部使用进程变量配置；必填值缺失会报错。
- YAML 顶层必须是映射，键使用小写 Settings 字段名，值支持字符串、数字、布尔、null；不支持嵌套字典或列表。空文件、未知字段和非标量值会被拒绝。
- 文件值转换为 `CLEANSIGHT_<大写字段名>`，仅填充不存在的进程变量，再由 Pydantic 校验。环境变量优先于文件，文件优先于字段默认值；YAML 的 null 跳过注入，不代表显式清空。
- 密码、token、设备编号建议加引号。空进程变量会占据覆盖位置，随后因 env_ignore_empty 被忽略，不会回退到文件值。YAML 空字符串也会在注入后被忽略。
- Settings 不热加载。修改 YAML 或进程环境后重启后端；启动脚本只激活环境并运行 python -m app.main，不接受角色参数，统一关闭 reload。
- 六个必填字段没有默认值，即使 strict 为 false 也不能省略；strict 控制额外的空值／零数据库端口检查，不能替代值域、连通性或业务验证。模板开启 strict，代码默认关闭。

配置文件与密钥不随源码提交。依赖和二进制由显式 /install 准备，运行配置、物料及启动验收由显式 /run 处理。

## 常规部署

数据库、告警目标、HTTP／RTSP 端口、设备编号、模型及存储目录由现场决定。白名单按访问来源填写；需要跨重启保留媒体 URL 时设置稳定的 `media_token_secret`。配置中不要留下示例凭据。模板不包含模型推理规则，规则仍由现有服务 YAML 管理。

| 字段 | 类型 | 代码默认值 | 含义 |
|---|---|---|---|
| `db_host` | str | 必填 | PostgreSQL 主机 |
| `db_port` | int | 必填 | PostgreSQL 端口 |
| `db_name` | str | 必填 | 数据库名 |
| `db_user` | str | 必填 | 数据库用户 |
| `db_password` | str | 必填 | 数据库密码，不在检查输出中展示 |
| `alarm_report_url` | str | 必填 | 告警上报目标，需核对所属环境 |
| `strict` | bool | false | 开启额外必需值检查，模板设置 true |
| `host` | str | `0.0.0.0` | HTTP 监听地址，不控制 MediaMTX 地址 |
| `port` | int | 8000 | HTTP TCP 端口 |
| `mediamtx_proxy_port` | int | 8004 | RTSP 对外代理 TCP 端口 |
| `mediamtx_internal_port` | int | 18004 | MediaMTX 内部 RTSP TCP 端口 |
| `mediamtx_rtp_port` | int | 8002 | MediaMTX RTP UDP 端口 |
| `mediamtx_rtcp_port` | int | 8003 | MediaMTX RTCP UDP 端口 |
| `cuda_device` | str | `"0"` | 子进程可见的设备编号；加载器目前不能保留空字符串，CPU 选择限制见下文 |
| `model_path` | str | `./app/data` | 模型根目录，供 inference YAML 的变量展开使用 |
| `storage_dir` | str | `./database` | 持久存储根目录，相对路径按项目根解析 |

## 高级覆盖项

| 用途 | 字段与默认值 | 调整时机 |
|---|---|---|
| 日志 | `debug: false`、`log_level: INFO` | 定位问题时启用详细日志 |
| 工具路径 | `ffmpeg_path: ""`、`mediamtx_bin: auto`、`mediamtx_config: config/mediamtx.yml` | 特殊安装位置；FFmpeg 默认使用项目归档版本，MediaMTX 纯代理选择限制见下文 |
| 帧率与采样 | `raw_fps: 30`、`inference_decimation: 2` | 调整输入速率与推理负载；推理帧率由两者派生 |
| 缓存 | `ca_maxlen_seconds: 20`、`ca_segment_seconds: 10`、`ca_ready_seconds: 2.0` | 内存及录制需求变化；不要孤立修改，需核对消费者约束 |
| 容量规划 | `capacity_planned_streams: 15`、`memory_baseline_mb: 6144`、`memory_check_enforce: true` | 核对 Linux 启动内存预算；规划路数不限制实际路数 |
| 渲染 | `viz_processes: 2` | 按 CPU 与并发规模调整；0 使用线程内渲染 |
| 拉流 | `rtsp_read_timeout_s: 2.5` | 网络与断流检测调优；核对 health_monitor 的清理超时 |
| 访问检查 | `gateway_enabled: true`、`gateway_allowed_ips: ""` | HTTP／RTSP 共用；关闭检查不会停止代理或 MediaMTX |
| 普通配额 | `gateway_rate_limit: 60`、`rtsp_rate_limit: 30`、`gateway_rate_window: 60` | 按合法 HTTP 请求和 RTSP 连接频率调整，各自计数 |
| 宽松配额 | `gateway_relaxed_rate_limit: 600` | 控制台与高频轮询配额，路径由网关代码维护 |
| 升级封禁 | `gateway_rate_ban_threshold: 5`、`gateway_rate_ban_window: 60`、`gateway_ban_duration: 3600` | 持续超限的处置策略；阈值 0 禁止升级封禁 |
| HTTP 反扫描 | `gateway_scan_threshold: 10`、`gateway_scan_window: 300` | 调整 404／405 探测容忍度，不作用于 RTSP |
| 媒体 URL | `media_token_secret: ""`、`media_token_ttl: 1800` | 空密钥在进程内随机生成；TTL 单位秒 |
| Label Studio | `label_studio_url: ""`、`label_studio_token: ""`、`label_studio_default_project_id: 0` | 使用送标功能时配置；URL 和默认项目也可通过页面保存，页面保存值优先，token 不经页面流转 |
| Lab 临时目录 | `lab_export_temp_dir: ""` | 空值使用存储目录下的 `.lab_exports`，需独立磁盘时覆盖 |

高级字段类型按值的形式识别：带引号或路径／名称为 str，true／false 为 bool，无小数的数字为 int；ca_ready_seconds、rtsp_read_timeout_s 为 float。环境变量统一由字段名转大写并加前缀，例如 rtsp_read_timeout_s → CLEANSIGHT_RTSP_READ_TIMEOUT_S。

## 路径、端口与跨模块约束

- storage_base_dir、config_dir、database_url、allowed_ips_set、inference_fps 是派生属性，不是 YAML 字段。inference_fps = raw_fps / inference_decimation，默认 15；不要另配检测帧率。
- 缓存帧数在 [session/config.py](../../app/services/session/config.py) 按秒数换算：raw／processed 容量乘 raw_fps，待推理容量乘 inference_fps。调整缓存与段长需满足该模块的校验；调整读取超时需同时核对健康监控清理预算。
- FFmpeg 默认解析项目内 .ffmpeg/bin/ffmpeg(.exe)，不回退 PATH。MediaMTX 的 auto 解析 .mediamtx/mediamtx(.exe)，其显式二进制和配置相对路径按项目根解析；其他路径不要假定都具有相同解析方式，启动入口统一切到项目根。
- [MediaMTX 配置加载器](../../app/daemons/mediamtx/config.py) 检查五个服务端口在 1–65535，三个 TCP 端口互不冲突、两个 UDP 端口互不冲突；跨 TCP／UDP 同号允许。这不检查实际端口占用，需由 /run 核对。
- 托管模式由 [worker](../../app/daemons/mediamtx/worker.py) 将内部 RTSP／RTP／RTCP 的 MTX_* 地址传给子进程，不依赖 shell 导出桥接。worker 支持空二进制路径只管理代理，外部 MediaMTX 必须能在内部目标端口提供服务。
- HTTP／RTSP 共用访问配置，各自持有计数与封禁状态。gateway_enabled 仅控制访问检查，不控制 daemon 生命周期。白名单匹配地址字符串，不应填写 CIDR 网段。
- 模型规则、录制、清理、健康监控配置继续位于各服务 YAML，参见 [SERVICE_CONFIG.md](SERVICE_CONFIG.md)。日志 dictConfig 固定为 config/logging.json；debug 控制数据库 SQL echo，与 log_level 的 root logger 级别不同。

### 已核验的空字符串加载限制

当前 YAML 经环境变量传入 Pydantic，env_ignore_empty 为 true。虽然下游约定空 cuda_device 隐藏 GPU、空 mediamtx_bin 启用纯代理，但经常规 YAML／进程变量入口写空字符串，会分别回退 `"0"` 和 `"auto"`。因此当前入口不能用空字符串选择这两个模式；这不是 /run 验收已通过的能力，需要后续修正加载语义并回归。本次配置精简不改变该行为。FFmpeg 等原本默认为空的字段不受此默认值差异影响。

## 模块内部策略与升级迁移

HTTP 宽松与绕过路径由 [app/gateway.py](../../app/gateway.py) 维护，不在运行配置中覆盖。Lab 导出使用 veryfast，单段最多 5 分钟、一次提交最多 30 分钟及 20 段；接口上限与实际校验共用 [Lab service](../../app/services/lab/service.py) 的内部常量。

升级前从已有 YAML 删除以下字段，否则未知字段检查会拒绝加载：

- `gateway_relaxed_prefixes`、`gateway_bypass_prefixes`。
- `lab_export_ffmpeg_preset`、`lab_export_max_clip_ms`、`lab_export_max_total_ms`、`lab_export_max_clips_per_submit`。

这些字段对应的旧 `CLEANSIGHT_*` 环境变量不再生效。原先使用自定义值的部署需要先核对与代码默认策略的差异。本次不自动读取或修改实际配置。

旧 .env* 升级时先确认正确来源，将大写 CLEANSIGHT_* 键转为小写字段名并按模板填写单份 YAML；旧独立网关 INI／GATEWAY_* 不再加载。listen_port、target_port 分别迁为 mediamtx_proxy_port、mediamtx_internal_port，allowed_ips 迁为 gateway_allowed_ips，rate_limit 迁为 rtsp_rate_limit，其余共用策略改用 gateway_* 字段。

## 代码与验证来源

- [Settings 解析、schema 与单例](../../app/settings.py)、[最小模板](../../config/settings.example.yaml)。
- [网关分档与访问控制](../../app/gateway.py)、[MediaMTX daemon](../../app/daemons/mediamtx/worker.py)。
- [Lab 导出策略](../../app/services/lab/service.py)、[页面运行配置优先级](../../app/services/lab/runtime_config.py)。
- [配置回归](../../tests/test_runtime_environment.py)、[网关回归](../../tests/test_gateway.py)、[Lab 回归](../../tests/test_lab_service.py)。
