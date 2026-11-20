> 更新时间：2026-11-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Gateway And MediaMTX

HTTP GatewayMiddleware 与 RTSP TCP 代理同属后端进程，RTSP 代理和 MediaMTX 子进程由 app/daemons/mediamtx 的 lifespan 管理。
二者共用 app/gateway.py 的 Store 实现和 Settings 策略字段，但各持独立计数与封禁状态。完整配置见 [SERVICE_SETTINGS.md](SERVICE_SETTINGS.md)。

## FastAPI GatewayMiddleware

对 HTTP 请求与 WebSocket 握手生效（`gateway_enabled=False` 时整体透传）。检查顺序：IP 白名单 / 封禁 → 限流 →
（仅 HTTP）响应码为 404 / 405 时计入反扫描。配置项见 [SERVICE_CONFIG.md](SERVICE_CONFIG.md)「Gateway 配置」。

### 路径按前缀分三档，优先级 bypass > relaxed > normal

| 档 | 限流 | 反扫描计数 | IP 白名单 / 封禁 | 默认前缀 |
|----|------|:-:|:-:|------|
| normal | `gateway_rate_limit`（60 / 60s）；窗口内超限 `gateway_rate_ban_threshold`（5）次即升级封禁 | 计 | 查 | 其余全部（含 `/api`、`/ai`、`/lab`、`/algorithm`） |
| relaxed | `gateway_relaxed_rate_limit`（600 / 60s），独立 bucket；不升级封禁 | 不计 | 查 | `/health`、`/task/message`、`/task/live`、`/task/history`、`/traceback`、`/admin/`、`/metrics`、`/ui-f3m8` |
| bypass | 跳过 | 不计 | 查 | `/media` |

前缀固定在 app/gateway.py 的 _RELAXED_PREFIXES／_BYPASS_PREFIXES，随路由代码维护，不接受 YAML／环境变量覆盖。配额仍来自 Settings。

反扫描：`_TRACKED_CODES = {404, 405}`，`gateway_scan_threshold=10` / `gateway_scan_window=300` → 300s 内 10 次即封
`gateway_ban_duration=3600s`。

### 各路径的归档判据

- **`/media` 独占 bypass**：段路径内嵌 HMAC token，验不过即 403，没有可枚举面；而播放时每段 + 并发 + 拖动回溯的频次会
  真打爆 600 / 窗。
- **`/traceback` 用 relaxed 而非 bypass**：`task_id` / `step_id` / `track` 明文可枚举，需要保留配额上限；但它的 404 是正常
  业务态（只落 raw 的 step 按默认 `track=processed` 查即 404），不能计入反扫描。`/task/history` 一次返回最多 10 个 task，
  播放端逐个 HEAD 探 playlist，若计数会正好撞线。
- **`/task/live`、`/task/history` 用 relaxed**：大屏跨 origin 轮询，CORS 预检 `OPTIONS` 与实际请求各计一次，3s 一轮即
  40 次 / 分，普通档撑不住。
- **`/admin/` 用 relaxed**：运维页高频轮询并带延迟测试突发；保留尾斜杠，避免 /admin.php、/administrator 等探测路径进入宽松档。
- **`/ui-f3m8` 用 relaxed**：控制台以 ES module 分文件加载，开页和刷新会产生集中资产请求。
- **`/algorithm` 留在 normal**：比色端点没有轮询需求。

## MediaMTX daemon（app/daemons/mediamtx/）

- 构造不启动后台工作；后端内存预算检查后进入 daemon lifespan，先启动、最后停止。托管启动 5 秒内确认内部 RTSP 可连接，失败回滚并阻止后端就绪。
- RTSP 每 IP 默认 30 次连接／60 秒，窗口内持续超限 5 次升级封禁；与 HTTP 独立计数。没有 HTTP 反扫描。gateway_enabled 统一控制两种协议的访问检查。
- 非零异常退出最多重启 5 次，退避为 2、4、8、16、30 秒；意外零退出直接标记失败。最终失败关闭 RTSP 入口，保留后端及离线任务；健康接口返回 503／degraded。重试状态和故障代码见 [health API](../api/health.md)。
- 停止时关闭连接并回收限流清理线程；子进程终止等待 5 秒后强制结束。起停支持重复调用。
- auto 二进制位于 .mediamtx/；配置来自 Settings，旧 GATEWAY_*／INI 不再加载。worker 支持纯代理，但常规配置入口的空字符串限制见 SERVICE_SETTINGS.md。
- Python 代码统一在 app/ 下，导入预算及覆盖率检查随 daemon 迁移，不再单独启动网关 Python 进程。

## MediaMTX 只开 RTSP，后端拉流绕过代理直连

- config/mediamtx.yml 只开 RTSP；内部 RTSP 与 RTP／RTCP 地址由 worker 通过子进程 MTX_* 设置为 127.0.0.1 和对应 Settings 端口，对外经代理。
- 后端拉 RTSP 时，若 URL 端口等于 `mediamtx_proxy_port`，`app/services/stream/service.py::_rewrite_rtsp_url` 把地址改写为
  `127.0.0.1:{mediamtx_internal_port}`，绕过 RTSPProxy 直连 MediaMTX。对外端口经 NAT 映射时必须 1:1，否则改写失效。

## 代码来源

- `app/gateway.py`（`GatewayMiddleware` / `RateLimitStore` / `AntiScanStore`）、`app/settings.py`（`gateway_*` 默认值）
- `app/daemons/mediamtx/{config,worker,proxy,instance,__init__}.py`、`app/main.py`
- `config/mediamtx.yml`、`pyproject.toml`
- `tests/test_gateway.py`、`tests/test_mediamtx_daemon.py`、`tests/test_rtsp_proxy.py`、`tests/test_import_hygiene.py`
