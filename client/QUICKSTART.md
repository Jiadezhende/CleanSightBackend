# 客户端快速开始指南

## 🎯 5分钟快速上手

### 1. 安装依赖

```bash
cd /Users/hmj/projects/CleanSightBackend/client
pip install -r requirements.txt
```

### 2. 启动服务器

```bash
# 在另一个终端
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload
```

### 3. 启动客户端

#### 方式A: 使用脚本（推荐）

```bash
# 启动命令行客户端
./run_client.sh cli

# 启动API服务
./run_client.sh api

# 运行测试
./run_client.sh test
```

#### 方式B: 直接运行

```bash
# 命令行客户端
python camera_client.py --client-id my_camera

# API服务
python camera_client_api.py
```

## 📖 常用命令

### 命令行客户端

```bash
# 基本使用
python camera_client.py --client-id camera_001

# 运行30秒后自动停止
python camera_client.py --client-id camera_001 --duration 30

# 使用高帧率
python camera_client.py --client-id camera_001 --fps 60

# 使用高分辨率
python camera_client.py --client-id camera_001 --width 1280 --height 720
```

### API服务

```bash
# 启动API服务
python camera_client_api.py

# 通过API启动摄像头
curl -X POST "http://localhost:8001/start" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "camera_001"}'

# 查看状态
curl "http://localhost:8001/status"

# 停止摄像头
curl -X POST "http://localhost:8001/stop"

# 查看API文档
open http://localhost:8001/docs
```

## 🔥 常见场景

### 场景1: 单个摄像头实时上传

```bash
# 终端1: 启动服务器
uvicorn app.main:app --reload

# 终端2: 启动客户端
python camera_client.py --client-id camera_001
```

### 场景2: 多个摄像头同时上传

```bash
# 终端1: 服务器
uvicorn app.main:app --reload

# 终端2: 摄像头1
python camera_client.py --client-id camera_001 --camera-id 0 &

# 终端3: 摄像头2
python camera_client.py --client-id camera_002 --camera-id 1 &

# 终端4: 摄像头3
python camera_client.py --client-id camera_003 --camera-id 2 &
```

### 场景3: 通过API控制摄像头

```bash
# 终端1: 服务器
uvicorn app.main:app --reload

# 终端2: API服务
python camera_client_api.py

# 终端3: 控制命令
# 启动
curl -X POST "http://localhost:8001/start" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "camera_001"}'

# 等待一段时间...

# 停止
curl -X POST "http://localhost:8001/stop"
```

### 场景4: 查看处理后的视频

```bash
# 终端1: 服务器
uvicorn app.main:app --reload

# 终端2: 上传（客户端）
python camera_client.py --client-id camera_001

# 终端3: 接收处理后的视频
cd ../test
python test_websocket_video.py --client-id camera_001 --preview
```

## 🧪 测试

```bash
# 完整测试
python test_camera_client.py

# 只测试命令行客户端
python test_camera_client.py --mode cli --duration 30

# 只测试API服务
python test_camera_client.py --mode api --duration 30
```

## 🎨 配置建议

| 场景 | 分辨率 | FPS | JPEG质量 |
|------|--------|-----|----------|
| 标准监控 | 640x480 | 30 | 70 |
| 高清监控 | 1280x720 | 30 | 75 |
| 节省带宽 | 480x360 | 15 | 60 |
| 高性能 | 640x480 | 60 | 70 |

## 📊 性能参考

在典型硬件上（Intel i5, 8GB RAM）：

- **640x480 @ 30fps**: CPU 5-10%, 带宽 1-2 Mbps
- **1280x720 @ 30fps**: CPU 10-15%, 带宽 2-4 Mbps
- **640x480 @ 60fps**: CPU 10-15%, 带宽 2-3 Mbps

## 🐛 问题排查

### 摄像头无法打开
```bash
# 检查摄像头设备
ls /dev/video*  # Linux
system_profiler SPCameraDataType  # macOS

# 尝试其他摄像头ID
python camera_client.py --client-id test --camera-id 1
```

### 连接失败
```bash
# 检查服务器是否运行
curl http://localhost:8000/

# 检查端口
netstat -an | grep 8000
```

### 帧率低
```bash
# 降低分辨率
python camera_client.py --client-id test --width 480 --height 360

# 降低JPEG质量
python camera_client.py --client-id test --jpeg-quality 60
```

## 📚 完整文档

详细文档请参考：`README.md`

## 💡 提示

- 使用唯一的`client_id`标识每个摄像头
- 建议使用API服务来远程控制摄像头
- 定期检查`/status`接口监控运行状态
- 按Ctrl+C可以优雅地停止客户端

---

**开始使用吧！** 🚀
