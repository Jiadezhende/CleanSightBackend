# CleanSight 快速开始指南

本文档提供 CleanSight 后端系统的快速开始指南，包括启动流程、API 调用示例和测试方法。

## 前置条件

- 已完成 [部署指南](DEPLOYMENT_GUIDE.md) 中的环境配置
- FFmpeg 已安装并在 PATH 中
- MediaMTX 已下载并可执行

---

## 完整启动流程

### 1. 启动 MediaMTX（终端 1）

```bash
# Windows
cd mediamtx_v1.15.4
.\mediamtx.exe

# Linux
cd mediamtx_v1.15.5_linux_amd64
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

# 1. 加载任务
response = requests.get("http://localhost:8000/ai/load_task/1")
print(response.json())

# 2. 启动 RTSP 流
data = {
    "client_id": "rtsp.test.1",
    "rtsp_url": "rtsp://localhost:8004/live/rtsp.test.1",
    "fps": 30
}
response = requests.post("http://localhost:8000/inspection/start_rtsp_stream", json=data)
print(response.json())

# 3. 连接 WebSocket 接收推理结果
ws = websocket.WebSocket()
ws.connect("ws://localhost:8000/ai/video?client_id=rtsp.test.1")

while True:
    frame_data = ws.recv()  # Base64 编码的 JPEG 帧
    # 处理frame_data...

# 4. 停止流
requests.post("http://localhost:8000/inspection/stop_rtsp_stream?client_id=rtsp.test.1")
```

### 主要 API 端点

| 端点 | 方法 | 功能 |
|------|------|------|
| `/ai/status` | GET | 查询 AI 服务状态 |
| `/ai/load_task/{task_id}` | GET | 加载清洗任务 |
| `/inspection/start_rtsp_stream` | POST | 启动 RTSP 流 |
| `/inspection/stop_rtsp_stream` | POST | 停止 RTSP 流 |
| `/ai/video` | WebSocket | 实时推理结果流 |
| `/task/status/{client_id}` | WebSocket | 任务状态更新 |

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
