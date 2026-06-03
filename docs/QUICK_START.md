# CleanSight 快速开始指南

本文档提供 CleanSight 后端系统的快速开始指南，包括启动流程、API 调用示例和测试方法。

## 前置条件

- 已完成 [部署指南](DEPLOYMENT_GUIDE.md) 中的环境配置
- FFmpeg 已安装并在 PATH 中
- MediaMTX 已下载并可执行

---

## 完整启动流程

### 1. 启动 MediaMTX（终端 1）

> MediaMTX 二进制不随 git 分发,首次需先获取:Linux 部署机由 `./install.sh` 从 `vendor/mediamtx/`
> 解出到 `mediamtx/`,详见 [部署指南](DEPLOYMENT_GUIDE.md#5-安装-mediamtx)。

```bash
# Windows
cd mediamtx
.\mediamtx.exe

# Linux
cd mediamtx
./mediamtx
```

### 2. 启动后端 API（终端 2）

```bash
# Windows
.\start_backend.ps1 dev

# Linux
./start_backend.sh dev
```

验证启动成功：访问 http://localhost:8000/docs

### 3. 推流测试视频（终端 3）

```bash
cd test
ffmpeg -re -i test_video.mp4 -c:v libx264 -preset veryfast -tune zerolatency -f flv rtmp://localhost:1935/live/test
```

### 4. 运行集成测试（终端 4）

```bash
python integration_tests/local_full_pipeline_rtsp.py --task_id 1 --duration 30
```

---

## API 调用示例

### 完整工作流

```python
import requests
import websocket

# 1. 一步启动：加载任务 + 启动流（统一接口）
data = {
    "task_id": 1,
    "rtsp_url": "rtsp://localhost:8004/live/rtsp.test.1",
    "fps": 30
}
response = requests.post("http://localhost:8000/api/start", json=data)
print(response.json())  # {"status":"success", "client_id":"rtsp.test.1", ...}
client_id = response.json()["client_id"]

# 2. 连接 WebSocket 接收推理结果
ws = websocket.WebSocket()
ws.connect(f"ws://localhost:8000/ai/video?client_id={client_id}")

while True:
    frame_data = ws.recv()  # Base64 编码的 JPEG 帧
    # 处理 frame_data...

# 3. 终止任务（完整清理：解码器 + 推理 + ClientManager）
requests.post(f"http://localhost:8000/api/terminate?client_id={client_id}")
```

### 主要 API 端点

| 端点 | 方法 | 功能 |
| --- | --- | --- |
| `/api/start` | POST | 统一启动（加载任务 + 拉流） |
| `/api/terminate` | POST | 统一终止（完整清理） |
| `/ai/video` | WebSocket | 实时推理结果流 |
| `/task/status/{client_id}` | WebSocket | 任务状态更新 |
| `/task/message/{client_id}` | GET / WebSocket (`/task/msg/`) | 前端实时消息（告警/检测） |
| `/task/{task_id}/alarms` | GET | 告警历史查询 |
| `/health/status` | GET | 系统整体状态（替代 `/ai/status`） |
| `/health/monitor/stats` | GET | 健康监控统计 |

> 所有 HTTP/WS 请求先经 `GatewayMiddleware`（IP 白名单 / 速率限制 / 反扫描），详见 [API_GATEWAY.md](API_GATEWAY.md)。对外 RTSP 端口 8004 也由 `mediamtx_gateway` 代理。

详细 API 文档见 [API 端点文档](API_ENDPOINTS.md)。

---

## 测试方法

### 本地测试

```bash
# 完整流程测试（30秒）
python integration_tests/local_full_pipeline_rtsp.py --task_id 1 --duration 30

# 无窗口模式
python integration_tests/local_full_pipeline_rtsp.py --task_id 1 --duration 30 --no-window
```

### 远程测试

```bash
# 远程服务器测试
python integration_tests/remote_full_pipeline_rtsp.py \
    --task_id 1 \
    --duration 60 \
    --server 117.50.241.174
```

### 压力测试

```bash
# 并发10个任务
python integration_tests/stress_test.py --max-tasks 10 --duration 60

# 清理残留进程
python integration_tests/cleanup_processes.py
```

### 断线重连测试

```bash
# 测试自动重连
python integration_tests/test_reconnect_success.py --task_id 1

# 测试超时清理
python integration_tests/test_reconnect_timeout.py --task_id 1
```

---

## 返回数据结构

### ProcessedFrame

```python
{
    "processed_frame_b64": "data:image/jpeg;base64,/9j/4AAQSkZJ...",
    "inference_result": {
        "detections": [...],
        "confidence": 0.95
    },
    "raw_timestamp": "2026-01-30T12:34:56",
    "metadata": {...}
}
```

### TaskStatusResponse

```python
{
    "task_id": 1,
    "status": "running",
    "cleaning_stage": "LEAK",
    "bending": false,
    "bubble_detected": true,
    "fully_submerged": true,
    "updated_at": "2026-01-30T12:34:56"
}
```

### InferenceResult

```python
{
    "detections": [
        {
            "class": "bubble",
            "confidence": 0.92,
            "bbox": [x, y, w, h]
        }
    ],
    "metadata": {"stage": "LEAK"}
}
```

---

## 相关文档

- [部署指南](DEPLOYMENT_GUIDE.md) - 环境配置
- [API 端点文档](API_ENDPOINTS.md) - 详细 API 说明
- [RTSP 流程说明](RTSP_FLOW.md) - RTSP 流处理细节

**最后更新**: 2026-01-30
