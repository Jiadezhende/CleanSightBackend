# 一次预检的检查要点

Agent 使用项目虚拟环境，按任务编写一次性检查命令，复用现有接口：

- `app.settings.read_runtime_config` 可在未配置凭据时读取 YAML；`Settings.model_fields` 提供字段定义，`get_settings()` 提供有效配置与 Pydantic 校验，`validate_ports(settings)` 校验有效端口。异常只报告字段／类型，不输出验证输入值。
- 调用现有服务配置加载器验证推理、流、录制、客户端、告警、清理和健康监控参数。确认必备配置文件存在，避免加载器的默认回退掩盖缺失；不重复维护通用扫描框架。
- 在线权重取自启用 detector／rule 的 model_path；离线物料复用 `app.services.inference.offline.segmentation.materials.check_materials(config)`。该接口只构造策略并核对声明的文件，不加载权重。缺失时列出准确路径，等待用户补齐；不为通过检查关闭模型或规则。
- 核对 logging JSON、MediaMTX 配置及实际二进制路径。复用已通过 /install 的依赖结果；对显式覆盖的二进制或安装后异常，执行版本命令确认可用。
- 在实际数据盘创建小型临时文件，验证写入、随机写及重命名后清理。确认持久性、磁盘空间、GPU／PPU 和共享内存满足实际负载。

用户要求启动时，在同一轮预检执行数据库只读连接及必需表检查，并核对有效端口、占用者和平台映射。只配置／检查的任务按用户要求决定是否连接外部资源。不要先跑静态预检，再重跑同一套检查只为补数据库步骤。

数据库可用 `SELECT 1` 及 PostgreSQL 的 `to_regclass('clean_task')`、`to_regclass('clean_alarm')` 验证；迁移和业务写入另按授权执行。捕获连接异常并脱敏，避免输出完整连接串。

权重文件存在不等于模型可加载，模型就绪留到启动后确认。针对检查失败按需深入诊断，修复后只复查相关项。
