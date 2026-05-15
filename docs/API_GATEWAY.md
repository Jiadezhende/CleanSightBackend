# API Gateway 安全层

> **版本**: 1.0
> **日期**: 2026-04-13
> **状态**: ✅ 已上线

CleanSight 在 4.13 上线了面向公网部署的两层安全网关，用来收敛对外端口、限制非授权访问、缓解路径/方法枚举扫描，并保证合法的高频轮询接口不被误封。

---

## 目录

- [整体架构](#整体架构)
- [Layer A：RTSP TCP 代理网关](#layer-artsp-tcp-代理网关)
- [Layer B：HTTP/WS 网关中间件](#layer-bhttpws-网关中间件)
- [配置项速查](#配置项速查)
- [MediaMTX 端口收敛](#mediamtx-端口收敛)
- [运维与故障排查](#运维与故障排查)

---

## 整体架构

```
                      +---------------------------------------------+
    外部客户端 ─────▶ |  Layer A: mediamtx_gateway (独立进程)       |
    (RTSP 8004)       |  - IP 白名单 / 动态封禁                     |
                      |  - per-IP 连接速率限制                      |
                      |  - 可选守护 MediaMTX 子进程（指数退避重启） |
                      +---------------------+-----------------------+
                                            │ TCP 转发
                                            ▼
                                   127.0.0.1:18004 (MediaMTX)

                      +---------------------------------------------+
    外部客户端 ─────▶ |  Layer B: GatewayMiddleware (ASGI 中间件)   |
    (HTTP/WS 8000)    |  - IP 白名单 / 动态封禁                     |
                      |  - 滑动窗口速率限制（普通 / 宽松两套）      |
                      |  - 反扫描（404/405 计数触发封禁）           |
                      +---------------------+-----------------------+
                                            │
                                            ▼
                                    FastAPI 业务路由
```

两层网关**完全解耦**，各自拥有独立的 `IPWhitelistStore` / `RateLimitStore` 实例，IP 封禁规则互不传递。

---

## Layer A：RTSP TCP 代理网关

**代码位置**：[mediamtx_gateway/](../mediamtx_gateway/)

### 定位

MediaMTX 本身没有足够细粒度的 IP 白名单与速率限制能力，且历史上直接对外暴露 8004 存在探测/刷流量风险。本代理在 MediaMTX 之前做一次 TCP 透明转发，只有通过白名单 + 速率检查的客户端才会被转发至真正的 MediaMTX 端口。

### 关键文件

| 文件 | 作用 |
|---|---|
| [mediamtx_gateway/main.py](../mediamtx_gateway/main.py) | 入口：加载配置、可选守护 MediaMTX 子进程、启动代理、信号处理 |
| [mediamtx_gateway/rtsp_proxy.py](../mediamtx_gateway/rtsp_proxy.py) | TCP 代理本体：双向 pipe 转发，拒绝时直接 abort |
| [mediamtx_gateway/config.ini](../mediamtx_gateway/config.ini) | 默认配置文件 |

### 部署拓扑

```
客户端 ──▶  0.0.0.0:8004 (RTSPProxy)  ──▶  127.0.0.1:18004 (MediaMTX)
```

前提：`mediamtx.yml` 中 `rtspAddress` 必须改为 `127.0.0.1:18004`，否则 MediaMTX 仍对外暴露，代理形同虚设。详见 [MediaMTX 端口收敛](#mediamtx-端口收敛)。

### 两种运行模式

- **托管模式**：`mediamtx_bin` 配置指向 MediaMTX 可执行文件，Gateway 以子进程拉起 MediaMTX，MediaMTX 崩溃时按 `2^n` 秒指数退避重启，最多 5 次；达到上限后 Gateway 自己也一起停止。
- **纯代理模式**：`mediamtx_bin` 留空。Gateway 只跑 TCP 代理，MediaMTX 由外部（如 systemd、容器）独立管理。适合生产环境。

### 连接处理流程（[rtsp_proxy.py:83-124](../mediamtx_gateway/rtsp_proxy.py#L83-L124)）

1. 取客户端 IP；
2. 白名单 + 封禁检查失败 → `transport.abort()`（发 RST，客户端立刻收到 `ConnectionResetError`）；
3. 速率检查失败 → 同上；
4. 连 MediaMTX 内部端口，**最多重试 10 次（每次 0.5s，总上限 5s）**，用于容忍 MediaMTX 冷启动或短暂重启；
5. 双向 pipe（65536 bytes chunk）透明转发。

### 配置优先级

**环境变量 `GATEWAY_*` > `config.ini` > 代码默认值**（[main.py:50-63](../mediamtx_gateway/main.py#L50-L63)）

| 键 | 默认 | 作用 |
|---|---|---|
| `mediamtx_bin` | 空 | 留空 = 纯代理模式；填写则 Gateway 拉起 MediaMTX 子进程 |
| `mediamtx_config` | `mediamtx.yml` | MediaMTX 配置文件路径 |
| `listen_port` | `8004` | 对外暴露的 RTSP 端口 |
| `target_port` | `18004` | MediaMTX 实际监听端口（须与 `mediamtx.yml/rtspAddress` 一致） |
| `allowed_ips` | 空 | 逗号分隔白名单，空 = 允许所有 |
| `rate_limit` | `30` | 窗口内最大连接数 |
| `rate_window` | `60` | 速率窗口（秒） |
| `ban_duration` | `3600` | 封禁时长（秒） |

### 启动方式

```bash
python mediamtx_gateway/main.py
# 或
python -m mediamtx_gateway.main
```

---

## Layer B：HTTP/WS 网关中间件

**代码位置**：[app/utils/gateway.py](../app/utils/gateway.py)

### 定位

作为 ASGI 原生中间件挂载在 FastAPI 前，对所有 HTTP 请求与 WebSocket 握手做 IP、速率、反扫描检查。之所以使用**原生 ASGI** 而非 `BaseHTTPMiddleware`，是为了避免 WebSocket 升级时的 body buffering 问题。

### 挂载方式（[app/main.py:83-90](../app/main.py#L83-L90)）

```python
app.add_middleware(CORSMiddleware, ...)
# Gateway 注册在 CORS 之后 → Starlette 逆序包装 → Gateway 最先执行
app.add_middleware(GatewayMiddleware)
```

中间件使用**双重检查锁**的懒初始化，避免与 `app.settings` 形成导入环。

### 三大 Store

#### 1. `IPWhitelistStore`（[gateway.py:32-68](../app/utils/gateway.py#L32-L68)）

- 静态白名单 `gateway_allowed_ips`（空 = 不限制）+ 动态封禁字典
- **动态封禁优先于白名单**：即使一个 IP 在白名单内，被 `ban()` 后在 `ban_duration` 秒内仍然会被拒绝
- 时间基准使用 `time.monotonic()`，不受系统时钟跳变影响
- 过期封禁在每次查询时顺带清理，避免无限增长

#### 2. `RateLimitStore`（[gateway.py:76-142](../app/utils/gateway.py#L76-L142)）

- per-IP 滑动窗口（`deque` 存时间戳）
- **速率超限升级封禁**：窗口 `ban_window` 内违规次数达到 `ban_threshold` 时，调用 `IPWhitelistStore.ban(ip)` 把 IP 加入动态封禁表
- `ban_threshold=0` 或 `ban_store=None` 时只返回 429，不升级为封禁
- 中间件为普通路径和宽松路径分别实例化两个 `RateLimitStore`，宽松 bucket 不传 `ban_store`，不会触发升级封禁

#### 3. `AntiScanStore`（[gateway.py:150-202](../app/utils/gateway.py#L150-L202)）

- 追踪 per-IP 的 **404/405** 响应次数
- 窗口内达到 `scan_threshold` 次 → 调用 `whitelist_store.ban(ip)`
- 404 必须追踪：扫描器典型手法就是批量探测 `/admin.asp`、`/wp-login.php` 等不存在路径
- 正常客户端的偶发 404/405（1-2 次，如前端 bug）不会触发封禁

### 请求处理流程（[gateway.py:277-331](../app/utils/gateway.py#L277-L331)）

```
ASGI scope
   │
   ├─ lifespan → 直接透传
   ├─ gateway_enabled=false → 直接透传
   │
   ▼
提取 IP（优先 X-Forwarded-For，回退 scope.client[0]）
   │
   ▼
路径分类：bypass > relaxed > normal（path.startswith 前缀匹配）
   │
   ▼
1) whitelist.is_allowed(ip) → 否则 403
2) 速率检查：
   - bypass 路径：完全跳过
   - 宽松路径：独立高配额 bucket
   - 普通路径：标准 bucket → 否则 429
3) WebSocket → 直接透传，不包装响应（路由层自行鉴权）
4) HTTP → 包装 send 拦截响应 status，仅普通路径时 antiscan.record_error()
```

### 宽松路径机制（关键设计点）

高频轮询接口（默认 `/health`、`/task/message`）具备两个特征：
- **吞吐量远超普通 API**（`/health` 甚至可能每秒若干次）
- **经常返回 404/405**（如任务过期、方法不匹配），若计入反扫描会误封正常业务

因此 `gateway_relaxed_prefixes` 中的前缀会走：
- 独立的 `_relaxed_ratelimit` bucket（`gateway_relaxed_rate_limit` 默认 600/60s）
- **不计入** 反扫描计数
- **不触发** 速率超限升级封禁（`_relaxed_ratelimit` 构造时未传 `ban_store`）

匹配使用 `str.startswith()` 前缀匹配，因此 `/health` 覆盖 `/health/monitor/stats` 等子路径。

### 绕过路径机制（bypass）

某些路由自带强鉴权（如 `/media/*` 走 HMAC-SHA256 token + 短 TTL），破不了 token 的流量发不进来，再叠加速率限制只会误伤合法 HLS 高频段请求。

`gateway_bypass_prefixes`（默认 `/media`）匹配的路径：

- **完全跳过** 速率限制（既不消耗普通 bucket，也不消耗宽松 bucket）
- **不计入** 反扫描计数
- 仅保留 IP 白名单/封禁检查

**前提**：bypass 前缀下的所有路由必须自行做强鉴权，否则等同于裸暴露。新增 bypass 路径前先确认这一点。

### IP 提取策略

`_extract_ip()`（[gateway.py:336-353](../app/utils/gateway.py#L336-L353)）优先读 `X-Forwarded-For`（为未来加 Nginx 反代做准备），回退到 `scope["client"][0]`。当前直连模式下使用后者。

---

## 配置项速查

所有配置项在 [app/settings.py:91-109](../app/settings.py#L91-L109)，对应环境变量前缀 `CLEANSIGHT_GATEWAY_*`（见 [.env.example](../.env.example)）。

| 配置 | 默认 | 作用 |
|---|---|---|
| `gateway_enabled` | `true` | 总开关，关闭后中间件对所有请求透传 |
| `gateway_allowed_ips` | `""` | 逗号分隔 IP 白名单，空 = 不限制 |
| `gateway_rate_limit` | `60` | 普通路径窗口内最大请求数 |
| `gateway_rate_window` | `60` | 速率窗口（秒），**普通 & 宽松共用** |
| `gateway_rate_ban_threshold` | `5` | 持续超限违规次数达到后封禁，`0` = 关闭升级 |
| `gateway_rate_ban_window` | `60` | 违规计数窗口（秒） |
| `gateway_relaxed_prefixes` | `/health,/task/message,/admin-f3m8,/metrics` | 宽松路径前缀（逗号分隔） |
| `gateway_relaxed_rate_limit` | `600` | 宽松路径窗口内最大请求数 |
| `gateway_bypass_prefixes` | `/media` | 完全绕过速率限制+反扫描的前缀（仅靠路由层 token 鉴权） |
| `gateway_scan_threshold` | `10` | 窗口内 404/405 次数达到后封禁 |
| `gateway_scan_window` | `300` | 反扫描计数窗口（秒） |
| `gateway_ban_duration` | `3600` | 封禁时长（秒），白名单被动封禁 + 速率升级封禁 + 反扫描封禁共用 |

**`mediamtx_proxy_port` / `mediamtx_internal_port`** 两个端口字段（[settings.py:87-89](../app/settings.py#L87-L89)）供后端内部拉流绕过 RTSPProxy 直连 MediaMTX 使用，与 Layer A 的 `listen_port`/`target_port` 要保持一致。

---

## MediaMTX 端口收敛

**文件**：[mediamtx_v1.15.4/mediamtx.yml](../mediamtx_v1.15.4/mediamtx.yml) / [mediamtx_v1.15.5_linux_amd64/mediamtx.yml](../mediamtx_v1.15.5_linux_amd64/mediamtx.yml)

- **RTSP**：`rtspAddress` 改为 `127.0.0.1:18004`（只绑本机回环，必须经 RTSPProxy）
- **RTMP / HLS / WebRTC / SRT**：均关闭（`rtmp: no`、`hls: no`、`webrtc: no`、`srt: no`）
- **RTP/RTCP**：绑定地址从 `0.0.0.0` 改为 `127.0.0.1`
- **rtspTransports**：移除 `multicast`，关闭组播端口

未使用协议关停后，暴露面从 6 个端口收敛到 1 个（8004 by Layer A）。

---

## 运维与故障排查

### 查询封禁状态

当前实现没有暴露封禁名单的查询接口。排查时通过日志关键字：

- `[Gateway] Auto-banned IP: <ip> for <s>s` —— 封禁生效
- `[Gateway] Blocked: <ip> <path>` —— 已封禁 IP 尝试再次访问
- `[Gateway] Rate limited: <ip> <path>` —— 触发 429（未升级封禁）
- `[Gateway] Rate-limit ban: <ip> ...` —— 持续超限触发升级封禁
- `[Gateway] Scan detected from <ip> ...` —— 反扫描触发封禁
- `[RTSPProxy] Blocked (whitelist/ban/rate limit): <ip>` —— Layer A 拒绝连接

### 临时放行 IP

目前没有管理接口，需要改 `gateway_allowed_ips` 配置后**重启后端**。封禁表是内存态，重启即清。

> 后续若增加管理接口，必须放到 `gateway_relaxed_prefixes` 之外（以便受速率限制保护），并增加额外鉴权。

### 测试环境绕过

开发/单元测试中若被误封，可设置 `CLEANSIGHT_GATEWAY_ENABLED=false` 跳过中间件。Layer A（RTSPProxy）需要通过内部端口 `127.0.0.1:18004` 直连 MediaMTX 绕过。

### 相关测试

- [tests/test_gateway.py](../tests/test_gateway.py) —— Layer B 全量单元测试（store 行为、中间件集成、宽松路径、升级封禁）
- [tests/test_mediamtx_gateway.py](../tests/test_mediamtx_gateway.py) —— Layer A 代理行为 + 配置加载
