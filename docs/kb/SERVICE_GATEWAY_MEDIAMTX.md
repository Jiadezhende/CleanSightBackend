> 更新时间：2026-08-02
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Gateway And MediaMTX

本仓库有两类 gateway：FastAPI ASGI GatewayMiddleware 和独立 MediaMTX RTSP Gateway。

## FastAPI GatewayMiddleware

位置：`app/utils/gateway.py`

能力：

- IP 白名单。
- 动态封禁。
- per-IP 滑动窗口速率限制。
- 持续超限升级封禁。
- 404/405 反扫描检测。
- 对 HTTP 和 WebSocket 握手均生效。

路径三档，优先级 `bypass > relaxed > normal`（前缀匹配，`app/settings.py` 定默认值）：

| 档 | 限流 | 反扫描计数(404/405) | IP 白名单/封禁 | 默认前缀 |
|----|------|:-:|:-:|------|
| normal | `gateway_rate_limit`（60/窗）；持续超限升级封禁 | 计 | 查 | 其余全部 |
| relaxed | `gateway_relaxed_rate_limit`（600/窗）；不升级封禁 | 不计 | 查 | `/health`、`/task/message`、`/task/live`、`/task/history`、`/traceback`、`/admin-f3m8`、`/metrics` |
| bypass | 完全跳过限流与反扫描 | 不计 | 查 | `/media` |

归档判据（别把路径塞错档）：

- **`/media` 独占 bypass**：段路径内嵌 HMAC token，验不过即 403，**无可枚举面**；且频次（每段+并发+拖进度条回溯）会真打爆 600/窗，限流对它冗余。`/media` 从来在 bypass、不在 relaxed。
- **`/traceback` 给 relaxed 而非 bypass**：参数 `task_id`/`step_id`/`track` 明文可枚举，bypass 等于放开任意频次扫描；它只需「404 不计封禁」——404 在此是**正常业务态**（只落 raw 的 step 按默认 `track=processed` 查即 404）。配额上限保留。
- **`/task/live`、`/task/history` 进 relaxed**：大屏跨 origin 轮询，浏览器 CORS 预检 `OPTIONS` 与实际请求各计一次，普通 60/窗 撑不住（3s 轮一次即 40 次/分）。
- 反扫描计数 `_TRACKED_CODES = {404, 405}`；`scan_threshold=10 / scan_window=300` → 300s 内 10 次即封 `ban_duration=3600s`。`/task/history` 一次最多返 10 个 task，播放栈逐个 HEAD 探 playlist 正好撞线——这是「补 HEAD（治 405）+ 进 relaxed（免计数）」合并止血的由来。

> relaxed/bypass 前缀默认值定在 `app/settings.py`（非仅 `.env`）——大屏自封是生产正确性问题，不依赖部署手工配置。

## MediaMTX Gateway

位置：`mediamtx_gateway/main.py`、`mediamtx_gateway/rtsp_proxy.py`

职责：

- 可选启动并守护 MediaMTX 子进程。
- 在 MediaMTX RTSP 端口前放置 TCP proxy。
- 使用 IP 白名单和速率限制保护 RTSP 入口。
- MediaMTX 异常退出时指数退避重启，最多 5 次。

配置优先级：

```text
GATEWAY_* 环境变量 > mediamtx_gateway/config.ini > 默认值
```

## MediaMTX 与后端拉流

MediaMTX 对外暴露 RTMP、RTSP、HLS、WebRTC 等端口（端口配置见 `mediamtx/mediamtx.yml`）。

主后端拉 RTSP 时，若请求 URL 端口等于 `mediamtx_proxy_port`，`StreamService` 会把 URL 改写到 `127.0.0.1:{mediamtx_internal_port}`，绕过 RTSPProxy。

## 代码来源

- `app/utils/gateway.py`
- `app/settings.py`
- `mediamtx_gateway/main.py`
- `mediamtx_gateway/rtsp_proxy.py`
- `mediamtx/mediamtx.yml`
- `tests/test_gateway.py`
- `tests/test_mediamtx_gateway.py`

