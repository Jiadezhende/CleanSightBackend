# 摄像头采集客户端

## 📋 概述

这是一个完整的摄像头采集客户端，可以从本地摄像头采集视频并通过WebSocket实时上传到CleanSight服务器。

提供两种使用方式：
1. **命令行客户端** (`camera_client.py`) - 直接运行
2. **API服务** (`camera_client_api.py`) - 通过HTTP API控制

## 🚀 快速开始

### 1. 安装依赖

```bash
# 进入客户端目录
cd client

# 安装Python依赖
pip install -r requirements.txt
```

或手动安装：
```bash
pip install opencv-python websockets fastapi uvicorn pydantic
```

### 2. 启动服务器

确保CleanSight服务器正在运行：
```bash
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload
```

## 📦 方式1: 命令行客户端

### 基本使用

```bash
cd client

# 启动摄像头采集（默认摄像头）
python camera_client.py --client-id my_camera_001

# 按 Ctrl+C 停止
```

### 高级选项

```bash
# 完整配置
python camera_client.py \
  --client-id my_camera_001 \
  --server-url ws://localhost:8000/inspection/upload_stream \
  --camera-id 0 \
  --fps 30 \
  --width 640 \
  --height 480 \
  --jpeg-quality 70

# 运行指定时长（秒）
python camera_client.py --client-id my_camera_001 --duration 60

# 使用外部摄像头
python camera_client.py --client-id my_camera_001 --camera-id 1

# 高帧率采集
python camera_client.py --client-id my_camera_001 --fps 60

# 高分辨率
python camera_client.py --client-id my_camera_001 --width 1920 --height 1080
```

### 参数说明

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--client-id` | `-c` | 客户端ID（必需） | - |
| `--server-url` | `-s` | WebSocket服务器地址 | `ws://localhost:8000/inspection/upload_stream` |
| `--camera-id` | | 摄像头ID | `0` |
| `--fps` | `-f` | 采集帧率 | `30` |
| `--width` | `-w` | 视频宽度 | `640` |
| `--height` | `-h` | 视频高度 | `480` |
| `--jpeg-quality` | `-q` | JPEG质量 (1-100) | `70` |
| `--duration` | `-d` | 运行时长（秒），0为无限 | `0` |

## 🌐 方式2: API服务（推荐）

### 启动API服务

```bash
cd client

# 启动API服务（默认端口8001）
python camera_client_api.py

# 自定义端口
python camera_client_api.py --port 8002

# 开发模式（热重载）
python camera_client_api.py --reload
```

服务启动后可以访问：
- API文档: http://localhost:8001/docs
- 备用文档: http://localhost:8001/redoc

### API接口

#### 1. 启动摄像头 - POST /start

启动摄像头采集和视频上传。

**请求示例：**
```bash
curl -X POST "http://localhost:8001/start" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "camera_001",
    "server_url": "ws://localhost:8000/inspection/upload_stream",
    "camera_id": 0,
    "fps": 30,
    "width": 640,
    "height": 480,
    "jpeg_quality": 70
  }'
```

**响应示例：**
```json
{
  "status": "success",
  "message": "摄像头已启动",
  "client_id": "camera_001",
  "server_url": "ws://localhost:8000/inspection/upload_stream",
  "camera_id": 0,
  "fps": 30
}
```

#### 2. 停止摄像头 - POST /stop

停止摄像头采集和视频上传。

**请求示例：**
```bash
curl -X POST "http://localhost:8001/stop"
```

**响应示例：**
```json
{
  "status": "success",
  "message": "摄像头已停止",
  "client_id": "camera_001",
  "final_stats": {
    "is_running": false,
    "elapsed_time": 30.5,
    "frames_sent": 915,
    "frames_success": 910,
    "frames_error": 5,
    "success_rate": 99.45,
    "average_fps": 30.0
  }
}
```

#### 3. 获取状态 - GET /status

获取当前客户端状态和统计信息。

**请求示例：**
```bash
curl "http://localhost:8001/status"
```

**响应示例：**
```json
{
  "is_running": true,
  "client_id": "camera_001",
  "elapsed_time": 15.2,
  "frames_sent": 456,
  "frames_success": 450,
  "frames_error": 6,
  "success_rate": 98.68,
  "average_fps": 30.0
}
```

#### 4. 健康检查 - GET /health

```bash
curl "http://localhost:8001/health"
```

## 🧪 测试

### 测试脚本

使用提供的测试脚本快速测试客户端：

```bash
cd client

# 测试命令行客户端（运行30秒）
python test_camera_client.py --mode cli --duration 30

# 测试API服务
python test_camera_client.py --mode api

# 完整测试
python test_camera_client.py --mode both
```

### 手动测试流程

#### 测试1: 命令行客户端

```bash
# 终端1: 启动服务器
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload

# 终端2: 启动客户端
cd /Users/hmj/projects/CleanSightBackend/client
python camera_client.py --client-id test_camera --duration 60

# 终端3: 接收处理后的视频（可选）
cd /Users/hmj/projects/CleanSightBackend/test
python3 test_websocket_video.py --client-id test_camera --duration 60
```

#### 测试2: API服务

```bash
# 终端1: 启动服务器
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload

# 终端2: 启动客户端API服务
cd /Users/hmj/projects/CleanSightBackend/client
python camera_client_api.py

# 终端3: 测试API
# 启动摄像头
curl -X POST "http://localhost:8001/start" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "test_camera"}'

# 查看状态
curl "http://localhost:8001/status"

# 停止摄像头
curl -X POST "http://localhost:8001/stop"
```

## 📊 预期输出

### 命令行客户端输出

```
============================================================
🚀 启动摄像头客户端
============================================================
Client ID: camera_001
服务器: ws://localhost:8000/inspection/upload_stream
摄像头ID: 0
目标FPS: 30
分辨率: 640x480
JPEG质量: 70
============================================================
✅ 摄像头初始化成功
   分辨率: 640x480
   帧率: 30 FPS
🔌 正在连接到服务器: ws://localhost:8000/inspection/upload_stream?client_id=camera_001
✅ WebSocket连接成功
✅ 客户端启动成功，开始采集和上传...
⏱️  按 Ctrl+C 停止客户端...
📊 发送: 30 帧 | 成功: 30 | 失败: 0 | FPS: 29.95
📊 发送: 60 帧 | 成功: 60 | 失败: 0 | FPS: 29.98
📊 发送: 90 帧 | 成功: 90 | 失败: 0 | FPS: 30.01
...

⚠️  收到中断信号
🛑 正在停止客户端...
✅ 摄像头已释放
🔌 WebSocket连接已关闭
============================================================
📊 客户端统计
============================================================
运行时长: 30.15 秒
发送帧数: 905
成功帧数: 900
失败帧数: 5
成功率: 99.45%
平均FPS: 30.02
============================================================
✅ 客户端已停止
```

### API服务输出

```
============================================================
🚀 启动摄像头客户端API服务
============================================================
地址: http://0.0.0.0:8001
API文档: http://0.0.0.0:8001/docs
重定向文档: http://0.0.0.0:8001/redoc
============================================================
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8001 (Press CTRL+C to quit)
```

## 🔧 配置建议

### 性能优化

```bash
# 推荐配置（平衡性能和质量）
python camera_client.py \
  --client-id camera_001 \
  --fps 30 \
  --width 640 \
  --height 480 \
  --jpeg-quality 70

# 高性能配置（优先速度）
python camera_client.py \
  --client-id camera_001 \
  --fps 30 \
  --width 480 \
  --height 360 \
  --jpeg-quality 60

# 高质量配置（优先清晰度）
python camera_client.py \
  --client-id camera_001 \
  --fps 30 \
  --width 1280 \
  --height 720 \
  --jpeg-quality 85
```

### 多摄像头配置

```bash
# 摄像头1
python camera_client.py --client-id camera_001 --camera-id 0 &

# 摄像头2
python camera_client.py --client-id camera_002 --camera-id 1 &

# 摄像头3
python camera_client.py --client-id camera_003 --camera-id 2 &
```

### 远程服务器

```bash
# 连接到远程服务器
python camera_client.py \
  --client-id camera_001 \
  --server-url ws://192.168.1.100:8000/inspection/upload_stream
```

## 🐛 故障排查

### 问题1: 无法打开摄像头

```
❌ 无法打开摄像头 0
```

**解决方案：**
- 检查摄像头是否被其他程序占用
- 尝试使用不同的摄像头ID：`--camera-id 1`
- 检查摄像头权限（macOS需要相机权限）

### 问题2: WebSocket连接失败

```
❌ WebSocket错误: [Errno 61] Connection refused
```

**解决方案：**
- 确保服务器正在运行：`uvicorn app.main:app --reload`
- 检查服务器地址和端口是否正确
- 检查防火墙设置

### 问题3: 帧率低

**解决方案：**
- 降低分辨率：`--width 480 --height 360`
- 降低JPEG质量：`--jpeg-quality 60`
- 降低目标帧率：`--fps 15`
- 检查CPU使用率

### 问题4: API服务启动失败

```
ERROR: [Errno 48] Address already in use
```

**解决方案：**
- 端口被占用，使用其他端口：`--port 8002`
- 或杀死占用端口的进程

## 📝 开发指南

### 集成到其他应用

```python
from camera_client import CameraClient

# 创建客户端
client = CameraClient(
    client_id="my_app_camera",
    server_url="ws://localhost:8000/inspection/upload_stream",
    camera_id=0,
    fps=30
)

# 启动采集
if client.start():
    print("客户端已启动")
    
    # 获取实时统计
    import time
    time.sleep(5)
    stats = client.get_stats()
    print(f"已发送 {stats['frames_sent']} 帧")
    
    # 停止采集
    client.stop()
```

### API集成

```python
import requests

# 启动摄像头
response = requests.post("http://localhost:8001/start", json={
    "client_id": "camera_001",
    "fps": 30
})
print(response.json())

# 获取状态
response = requests.get("http://localhost:8001/status")
print(response.json())

# 停止摄像头
response = requests.post("http://localhost:8001/stop")
print(response.json())
```

## 📚 更多信息

- 服务端文档: `/Users/hmj/projects/CleanSightBackend/README.md`
- WebSocket测试: `/Users/hmj/projects/CleanSightBackend/test/QUICKSTART.md`
- API文档: 启动API服务后访问 http://localhost:8001/docs

## 🎯 最佳实践

1. **使用API服务** - 更易于集成和管理
2. **合适的分辨率** - 640x480适合大多数场景
3. **JPEG质量70** - 平衡性能和质量
4. **监控统计信息** - 定期检查success_rate和fps
5. **唯一的client_id** - 每个摄像头使用不同的ID
6. **异常处理** - 实现自动重连机制

## ✅ 功能清单

- [x] 摄像头采集
- [x] WebSocket视频上传
- [x] 异步高性能传输
- [x] 帧率控制
- [x] JPEG压缩
- [x] 实时统计
- [x] REST API控制
- [x] 健康检查
- [x] 优雅停止
- [x] 多摄像头支持
- [x] 远程服务器支持
- [x] 完整文档

---

**祝使用愉快！** 🎉
