> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Stream Service

流服务负责为每个 client 管理 FFmpeg 解码器，并把视频帧写入 ClientQueues。

## 主要职责

- 注册、启动、停止和重启每个 `client_id` 的 decoder。
- 构建 RTSP/RTMP 协议选项。
- 将对外 RTSP proxy 端口改写为 MediaMTX 内部端口。
- 维护 decoder 字典和基础 metrics。
- 在 POSIX 上用 selector 消费 stdout。

## URL 重写

`_rewrite_rtsp_url()` 只在 URL 端口等于 `settings.mediamtx_proxy_port` 时生效。

重写后：

- host 固定为 `127.0.0.1`
- port 改为 `settings.mediamtx_internal_port`
- userinfo 保留

目的：后端拉流绕过 RTSPProxy，直连本机 MediaMTX 内部端口。

## FFmpeg 解码

`FFmpegDecoder` 构造命令：

- 输入：`stream_url`
- 输出：`pipe:1`
- 格式：`rawvideo`
- 像素格式：默认 `bgr24`
- 尺寸：默认 640x480
- fps：默认 30

解码后写入：

- `ca_raw`
- `ca_ready`
- `latest_raw_frame/latest_raw_timestamp`

## 背压

Decoder 配置有 `backpressure_ratio`，ClientQueues 的 `ca_ready` 也有 maxlen。`append_ca_ready_with_throttle()` 按 inference fps 限流，队列满时拒绝写入。

## 健康监控输入

健康监控通过 `latest_raw_timestamp` 判断断流，通过 StreamService 查询和重启 decoder。

## 代码来源

- `app/services/stream/service.py`
- `app/services/stream/decoder.py`
- `app/services/stream/config.py`
- `config/stream_config.yaml`
- `tests/test_stream_rewrite.py`
- `tests/test_reconnect_on_initial_failure.py`

