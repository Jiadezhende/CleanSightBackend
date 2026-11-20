# 运行配置

每个部署目录使用一份 `config/settings.yaml`，模板为 `config/settings.example.yaml`，字段定义与校验复用 `app/settings.py`。文件使用平铺的小写字段名，`CLEANSIGHT_*` 进程环境变量优先覆盖。没有环境选择参数，旧 `.env*` 不再自动读取。

缺配置时 Agent 按目标平台生成安全创建命令：从模板复制，目标已存在则保留；需要写文件时使用排他创建，Linux 限制为当前用户可读写。已有旧配置迁移前先确认来源与目标，不直接覆盖。

用户在目标机填写数据库和告警凭据，检查只输出字段缺失／占位状态，不输出值。必需字段取自 Settings，避免维护另一份字段清单。非敏感路径、端口和运行参数按用户用途调整。

- 确认数据库与告警端点属于预期目标。开发验证使用开发资源；无需真实上报时可使用 alarm.invalid，并报告外部上报未验证。
- 端口、日志、严格模式、访问控制由配置值决定。入口会将端口同步给后端、网关与 MediaMTX；检查已有进程变量覆盖。
- 按需要设置模型与持久存储目录、共享设备卡号和实际规划路数。PPU 每个新 shell 加载厂商 SDK。内存自检失败先核对资源和规划。
- 需要重启后媒体 URL 保持有效时配置持久媒体密钥，在目标机生成并直接写文件，已有密钥保留。

其余参数保留默认值，只在明确用途或故障证据下修改。

HTTP 与 RTSP 共用 gateway_enabled、gateway_allowed_ips、gateway_rate_window、gateway_rate_ban_threshold、gateway_rate_ban_window、gateway_ban_duration。HTTP 使用 gateway_rate_limit，RTSP 使用 rtsp_rate_limit；计数与封禁状态各自维护。开关控制访问检查，daemon 起停仍由 lifespan 管理。mediamtx_bin 默认 auto，空字符串表示外部 MediaMTX、仅启动代理；mediamtx_config 默认 config/mediamtx.yml。
