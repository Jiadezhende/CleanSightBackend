# 统一 YAML 运行配置、增加 run 技能并迁入 MediaMTX daemon

> **变更状态**：生效中（2026-11-20）
> **知识库**：待沉淀

## 概述

新增显式 /run 技能，运行配置统一为 config/settings.yaml。RTSP 代理与 MediaMTX 守护迁入 daemon，由后端 lifespan 统一管理；合并通用网关策略，增加故障健康状态。

## 变更背景

/install 已独立提交，职责仅为依赖安装。运行目录只需一份配置，原 dev／test／prod 文件选择和启动脚本端口桥接不再适用。MediaMTX 原本独立运行，配置和生命周期与后端分离；本次统一为现有 daemon 风格。

## 方案详情

整体流程为 /install 准备依赖 → /run 准备配置及核对物料 → 启动脚本运行 app.main → lifespan 启停 MediaMTX 与业务服务 → 健康路由读取状态。配置和 daemon 共用启动入口，本次作为一个可运行的整体提交。

| 部件 | 实现位置 |
|---|---|
| 运行配置与预检指导 | .claude/skills/run/ |
| YAML 解析、字段校验与单例 | app/settings.py |
| RTSP 代理及进程守护 | app/daemons/mediamtx/ |
| 生命周期与健康状态 | app/main.py、app/routers/health.py |

### 运行配置与技能

/run 仅在用户显式调用时启用，使用 disable-model-invocation 开关。流程为准备配置、一次预检、按授权启动验收；Agent 根据平台生成临时命令，复用现有解析器，不增加固定检查脚本。启动任务检查数据库、端口、模型权重和服务配置，业务验证遵循用户授权。

每个部署目录使用一份 config/settings.yaml，模板为 config/settings.example.yaml。app.settings 使用已有 PyYAML 的 safe_load，字段为 Settings 小写名称，支持标量类型；拒绝未知字段、非法键和非标量值。PyYAML 补为直接依赖。值转换为 CLEANSIGHT_* 供 Settings 和服务配置变量展开共用，已有进程变量优先，Pydantic 负责类型和必需字段校验。

移除 CLEANSIGHT_ENV 文件路由，旧 .env／.env.dev／.env.prod／.env.test 不再自动加载，忽略规则保留。严格检查直接遵从 strict，无开发角色绕过。配置实例首次使用时创建，保留 from app.settings import settings 接口；单独导入 schema 和解析函数无需运行凭据。

启动脚本仅激活环境并运行 python -m app.main，不接受角色参数；所有运行环境关闭 reload。端口来自 Settings，worker 给子进程生成 MTX_* 地址，无脚本导出桥接。

### 结构和网关配置

包根提供 lifespan，config 从 Settings 解析，instance 构造不启动线程／socket，worker 管理状态和子进程，proxy 管理连接与转发。复用 IPWhitelistStore／RateLimitStore；后者增加 close，使 daemon 停止时可回收清理线程。

HTTP／RTSP 共用 gateway_enabled、gateway_allowed_ips、gateway_rate_window、gateway_rate_ban_threshold、gateway_rate_ban_window、gateway_ban_duration；分别使用 gateway_rate_limit（默认 60）和 rtsp_rate_limit（默认 30）。两种协议计数与封禁状态独立，RTSP 连续超限也会升级封禁。关闭访问检查不停止代理和 MediaMTX。

mediamtx_bin 默认 auto，指向项目 .mediamtx 中对应平台二进制；空字符串运行纯代理。mediamtx_config 默认 config/mediamtx.yml。配置、显式相对路径均以项目根目录解析。

### 生命周期和健康

内存预算检查后先启动本 daemon，最后停止，覆盖内部任务和录制的完整生命周期。托管模式启动后 5 秒内探测内部 RTSP 连接，失败回滚并阻止应用就绪。子进程非零退出最多重启 5 次，退避为 2、4、8、16、30 秒；意外零退出直接标记失败。纯代理模式不检查外部服务启动。

最终失败关闭 RTSP 接受入口与连接，清理限流线程和子进程；健康接口返回 503／degraded，保留后端与离线任务。状态含 state、managed、restart_count、error_code，重试期间记录 restarting，正常时 200。控制台消费 503 响应体保留监控信息，并继续记录轮询失败。

停止唤醒退避，关闭代理连接，终止子进程；5 秒未退出则 kill 并等待回收。起停可重复。内存和进程状态仅留在 daemon 内，路由只读状态。纯代理 running 仅表示代理已监听，外部 MediaMTX 和模型仍由 /run 验证。

### 迁移

实际运行配置未读取、修改或自动迁移。升级时由用户确认来源，按模板将旧环境文件转为 config/settings.yaml；原角色启动参数移除。同机实例需要显式分配各自的端口、数据库及存储。

根目录 mediamtx_gateway 与 config.ini 移除，不再单独运行网关 Python 进程。原 INI／GATEWAY_* 设置按下表转到 config/settings.yaml；原值由用户迁移，本轮不读取实际配置或自动覆盖。

| 旧字段／环境变量 | 新 Settings 字段 |
|---|---|
| mediamtx_bin / GATEWAY_MEDIAMTX_BIN | mediamtx_bin |
| mediamtx_config / GATEWAY_MEDIAMTX_CONFIG | mediamtx_config |
| listen_port / GATEWAY_LISTEN_PORT | mediamtx_proxy_port |
| target_port / GATEWAY_TARGET_PORT | mediamtx_internal_port |
| allowed_ips / GATEWAY_ALLOWED_IPS | gateway_allowed_ips |
| rate_limit / GATEWAY_RATE_LIMIT | rtsp_rate_limit |
| rate_window / GATEWAY_RATE_WINDOW | gateway_rate_window |
| ban_duration / GATEWAY_BAN_DURATION | gateway_ban_duration |

HTTP 的宽松路径与反扫描仍保留原行为。升级后缺少 MediaMTX 二进制或配置会阻止完整后端启动，运行 /install 准备物料；明确使用外部服务时配置 mediamtx_bin 为空。

## 变更效果与验证

模拟子进程与临时 socket 覆盖启动失败／取消回滚、进程回收、强制结束、崩溃重启、退避中停止、重试耗尽、纯代理、重复起停、访问开关、封禁和连接清理。验证健康 200／503 及其他路由可用，导入预算与分层门禁随迁移更新。

全量单元／组件测试 1276 项通过。控制台 503 响应保留、故障恢复及普通错误处理通过 Node 检查；Bash／PowerShell 语法、Settings 模板与技能引用检查通过。未启动真实 MediaMTX、连接真实业务库或运行实际推理。

配置回归覆盖 YAML 类型／形状、未知字段、进程变量优先、旧角色不选文件、严格模式、端口范围／冲突、按需实例化及单例接口。测试屏蔽实际运行配置和环境文件，提供不可达占位连接信息，并使用带 main 保护的 Windows spawn 入口。

## 遗留风险 / 后续任务

尚未执行跨 Python 3.10–3.12 安装、NVIDIA／PPU 真机验证、实际 MediaMTX 启动和模型推理。真实环境升级需先迁移配置并准备二进制与模型，再通过 /run 验收；模拟子进程和本地临时端口测试不能替代该步骤。
