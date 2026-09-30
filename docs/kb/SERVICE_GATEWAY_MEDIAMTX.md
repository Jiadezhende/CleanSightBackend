> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Gateway And MediaMTX

仓库有两个 gateway：后端进程内的 FastAPI `GatewayMiddleware`，和独立进程 `mediamtx_gateway`（RTSP TCP 代理）。
二者共用 `app/gateway.py` 的 `IPWhitelistStore` / `RateLimitStore` 实现，但各持独立实例，IP 规则与封禁互不影响。
`app/gateway.py` 放在 `app/` 根，正因为两个进程都要用它。

## FastAPI GatewayMiddleware

对 HTTP 请求与 WebSocket 握手生效（`gateway_enabled=False` 时整体透传）。检查顺序：IP 白名单 / 封禁 → 限流 →
（仅 HTTP）响应码为 404 / 405 时计入反扫描。配置项见 [SERVICE_CONFIG.md](SERVICE_CONFIG.md)「Gateway 配置」。

### 路径按前缀分三档，优先级 bypass > relaxed > normal

| 档 | 限流 | 反扫描计数 | IP 白名单 / 封禁 | 默认前缀 |
|----|------|:-:|:-:|------|
| normal | `gateway_rate_limit`（60 / 60s）；窗口内超限 `gateway_rate_ban_threshold`（5）次即升级封禁 | 计 | 查 | 其余全部（含 `/api`、`/ai`、`/lab-f3m8`、`/algorithm`） |
| relaxed | `gateway_relaxed_rate_limit`（600 / 60s），独立 bucket；不升级封禁 | 不计 | 查 | `/health`、`/task/message`、`/task/live`、`/task/history`、`/traceback`、`/admin-f3m8`、`/ui-f3m8`、`/metrics` |
| bypass | 跳过 | 不计 | 查 | `/media` |

前缀默认值定在 `app/settings.py`（`.env` 可覆盖），大屏自封是生产正确性问题，不依赖部署手工配置。

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
- **`/ui-f3m8` 用 relaxed**：一次开页拉 index.html 加多个 vendor 资产，普通档下刷新几次就会升级封禁；访问根 `/ui-f3m8/`
  的 404 也不再计入反扫描。
- **`/algorithm` 留在 normal**：比色端点没有轮询需求。

## MediaMTX Gateway（`mediamtx_gateway/`）

- **职责**：可选拉起并守护 MediaMTX 子进程；在 MediaMTX RTSP 端口前放 TCP 代理（`rtsp_proxy.py::RTSPProxy`），对入站连接
  查 IP 白名单与限流；MediaMTX 异常退出时指数退避重启（`min(2^n, 30)` 秒，最多 5 次，超限停网关）。
- **防护比后端弱**：只有白名单 + 每 IP 连接限流（默认 30 次 / 60s），`RateLimitStore` 未挂 ban_store，超限只拒绝、不升级
  封禁；没有反扫描。
- **配置优先级**：`GATEWAY_*` 环境变量 > `mediamtx_gateway/config.ini` > 代码默认值。`mediamtx_bin = auto` 按平台选
  `mediamtx/mediamtx(.exe)`，留空为纯代理模式。
- **启动方式只有一种**：在仓库根执行 `python -m mediamtx_gateway.main`（从 `app.gateway` 取 Store，需要仓库根在
  `sys.path` 上）。模块 import 无副作用，`logging.basicConfig` 在 `_main()` 里。
- **与后端同一套工具链**：导入门禁 `test_intra_package_relative_cross_package_absolute` 覆盖 `mediamtx_gateway/*.py`；覆盖率
  `source = ["app", "mediamtx_gateway"]`（`pyproject.toml`），须用不带值的 `pytest tests/ --cov`，`--cov=app` 会漏掉网关。

## MediaMTX 只开 RTSP，后端拉流绕过代理直连

- `mediamtx/mediamtx.yml` 里 `rtmp` / `hls` / `webrtc` / `srt` / `api` / `metrics` / `playback` 均为 `no`，只开 RTSP；
  `rtspAddress` 与 RTP / RTCP 地址都绑 `127.0.0.1`（端口由启动脚本经 `MTX_*` 覆盖），对外只经网关代理。
- 后端拉 RTSP 时，若 URL 端口等于 `mediamtx_proxy_port`，`app/services/stream/service.py::_rewrite_rtsp_url` 把地址改写为
  `127.0.0.1:{mediamtx_internal_port}`，绕过 RTSPProxy 直连 MediaMTX。对外端口经 NAT 映射时必须 1:1，否则改写失效。

## 代码来源

- `app/gateway.py`（`GatewayMiddleware` / `RateLimitStore` / `AntiScanStore`）、`app/settings.py`（`gateway_*` 默认值）
- `mediamtx_gateway/main.py`（`_load_config` / `_run_mediamtx` / `_main`）、`mediamtx_gateway/rtsp_proxy.py`、`mediamtx_gateway/config.ini`
- `mediamtx/mediamtx.yml`、`pyproject.toml`
- `tests/test_gateway.py`、`tests/test_mediamtx_gateway.py`、`tests/test_import_hygiene.py`
