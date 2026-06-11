> 更新时间：2026-05-24
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

路径分类：

- normal：标准限流和反扫描。
- relaxed：宽松限流，不做封禁升级和反扫描，用于 health、task message、admin、metrics 等高频接口。
- bypass：跳过速率限制和反扫描，但仍检查 IP 白名单/封禁；默认用于 `/media`。

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

