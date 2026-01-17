# 解码器进程池快速开始指南

## 快速概览

本项目已升级为基于**进程池**的FFmpeg解码架构，可以充分利用16核CPU并行处理多个视频流。

## 核心变化

### 之前（线程模式）
- 使用Python线程处理视频流
- 受GIL限制，无法真正并行
- 最多处理几个流

### 现在（进程池模式）
- 使用独立进程处理每个视频流
- 真正的多核并行，不受GIL限制
- 可同时处理最多16个流

## 快速使用

### 1. 启动后端服务

```bash
cd e:\ywc_college\junior1\本科生课题\src\CleanSightBackend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. 启动视频流捕获

**使用curl:**
```bash
curl -X POST http://localhost:8000/inspection/start_rtsp_stream \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "camera_001",
    "rtsp_url": "rtsp://localhost:8554/live/stream1",
    "fps": 30
  }'
```

**使用Python:**
```python
import requests

response = requests.post(
    "http://localhost:8000/inspection/start_rtsp_stream",
    json={
        "client_id": "camera_001",
        "rtsp_url": "rtsp://localhost:8554/live/stream1",
        "fps": 30
    }
)
print(response.json())
```

### 3. 检查运行状态

```bash
curl http://localhost:8000/inspection/decoder_stats
```

输出示例：
```json
{
  "total_processes": 1,
  "alive_processes": 1,
  "max_workers": 16,
  "queue_size": 15,
  "clients": ["camera_001"]
}
```

### 4. 停止视频流捕获

```bash
curl -X POST "http://localhost:8000/inspection/stop_stream?client_id=camera_001"
```

## 常用场景

### 场景1: 同时处理多个摄像头

```python
import requests

# 启动多个流
cameras = [
    {"id": "camera_001", "url": "rtsp://192.168.1.101:8554/live"},
    {"id": "camera_002", "url": "rtsp://192.168.1.102:8554/live"},
    {"id": "camera_003", "url": "rtsp://192.168.1.103:8554/live"},
]

for cam in cameras:
    response = requests.post(
        "http://localhost:8000/inspection/start_rtsp_stream",
        json={
            "client_id": cam["id"],
            "rtsp_url": cam["url"],
            "fps": 30
        }
    )
    print(f"{cam['id']}: {response.json()['message']}")
```

### 场景2: 监控进程池状态

```python
import requests
import time

while True:
    stats = requests.get("http://localhost:8000/inspection/decoder_stats").json()
    print(f"活动进程: {stats['alive_processes']}/{stats['max_workers']}")
    print(f"队列大小: {stats['queue_size']}")
    print(f"客户端: {stats['clients']}")
    time.sleep(5)
```

### 场景3: 批量停止所有流

```python
import requests

# 获取所有活动客户端
stats = requests.get("http://localhost:8000/inspection/decoder_stats").json()

# 停止所有流
for client_id in stats["clients"]:
    response = requests.post(
        "http://localhost:8000/inspection/stop_stream",
        params={"client_id": client_id}
    )
    print(f"停止 {client_id}: {response.json()['message']}")
```

## API端点总览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/inspection/start_rtsp_stream` | POST | 启动RTSP流捕获 |
| `/inspection/start_rtmp_stream` | POST | 启动RTMP流捕获 |
| `/inspection/stop_rtsp_stream` | POST | 停止RTSP流 |
| `/inspection/stop_rtmp_stream` | POST | 停止RTMP流 |
| `/inspection/stop_stream` | POST | 通用停止接口 |
| `/inspection/decoder_stats` | GET | 获取进程池统计信息 |

## 配置调整

### 修改最大进程数

编辑 [decoder.py](../app/services/decoder.py):

```python
PROCESS_POOL_SIZE = 16  # 改为你的CPU核心数
```

### 修改队列大小

编辑 [decoder.py](../app/services/decoder.py):

```python
self.frame_queue = Queue(maxsize=1000)  # 改为所需的队列大小
```

### 设置环境变量

```bash
# Windows PowerShell
$env:FFMPEG_PATH = "C:\path\to\ffmpeg.exe"
$env:MODEL_INPUT_WIDTH = "640"
$env:MODEL_INPUT_HEIGHT = "480"

# Linux/Mac
export FFMPEG_PATH="/usr/bin/ffmpeg"
export MODEL_INPUT_WIDTH="640"
export MODEL_INPUT_HEIGHT="480"
```

## 运行测试

```bash
cd test
python test_decoder_pool.py
```

测试将验证：
- ✓ 解码器统计信息获取
- ✓ 流的启动和停止
- ✓ 进程池状态检查
- ✓ 多流并发处理

## 性能监控

### 查看CPU使用率

**Windows:**
```powershell
# 查看Python进程CPU使用率
Get-Process python | Select-Object Name,CPU,WorkingSet
```

**Linux:**
```bash
# 查看所有python进程
top -p $(pgrep -d',' python)
```

### 查看内存使用

检查队列积压情况：
```python
import requests

stats = requests.get("http://localhost:8000/inspection/decoder_stats").json()
queue_size = stats["queue_size"]

if queue_size > 500:
    print("⚠️ 队列积压，AI推理可能跟不上解码速度")
```

## 故障排查

### 问题：进程无法启动

**检查1**: 是否已达到最大进程数？
```bash
curl http://localhost:8000/inspection/decoder_stats
```

**检查2**: FFmpeg是否可用？
```bash
ffmpeg -version
```

### 问题：帧不更新

**检查1**: 流URL是否可访问？
```bash
ffplay rtsp://your-stream-url  # 测试流是否可播放
```

**检查2**: 查看后端日志
```bash
# 查看解码器日志
# 应该看到类似 "[Decoder-camera_001] 已解码 30 帧" 的输出
```

### 问题：队列积压

**原因**: AI推理速度慢于解码速度

**解决方案**:
1. 降低视频流帧率
2. 优化AI推理代码
3. 使用GPU加速推理
4. 减小队列maxsize（会导致更多丢帧）

## 性能建议

### 1. CPU核心分配
- **推荐**: 进程池大小 = CPU核心数 - 2
- **示例**: 16核CPU → 设置为14个进程
- **原因**: 留出核心给操作系统和AI推理

### 2. 内存优化
- 每个进程约占用50-100MB内存
- 队列中每帧约1MB (640x480x3)
- **示例**: 10个流 + 1000帧队列 ≈ 1-2GB内存

### 3. 网络带宽
- 1080p@30fps ≈ 5-10 Mbps
- 720p@30fps ≈ 2-5 Mbps
- **示例**: 10个720p流 ≈ 20-50 Mbps

## 下一步

- 📖 阅读完整架构文档: [DECODER_POOL_ARCHITECTURE.md](DECODER_POOL_ARCHITECTURE.md)
- 🧪 运行测试: `python test/test_decoder_pool.py`
- 📊 查看API文档: `http://localhost:8000/docs`

## 技术支持

遇到问题？检查以下资源：
1. [架构文档](DECODER_POOL_ARCHITECTURE.md)
2. [FFmpeg安装指南](../FFMPEG_INSTALL.md)
3. [RTSP流配置](../MEDIAMTX_SETUP.md)
