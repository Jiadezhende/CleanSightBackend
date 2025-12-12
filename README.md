# CleanSightBackend

CleanSight 是一个用于长海医院内镜清洗过程 AI 检测的后端系统。它确保每个清洗步骤都正确执行，从而提高患者安全性和合规性。

## 功能简介

- **实时视频流处理**: 捕获视频，使用 AI 模型处理，并通过 WebSocket 推送结果。
- **三线程架构**: 解耦帧捕获、AI 推理和 WebSocket 推送，优化性能。
- **多任务并行推理**: 支持多种 AI 模型并行执行（关键点检测、动作分析、内镜弯折检测等）。
- **可扩展架构**: 基于任务注册表的设计，便于添加新的检测任务。
- **RTMP 流处理**: 从 RTMP 流以固定帧率提取视频帧，支持实时监控。
- **AI 推理**: 关键点检测 + 动作分析，实时评估清洗过程。
- **实时推送**: 通过 WebSocket 推送处理后的视频帧和推理结果。
- **视频追溯**: 自动生成 HLS 视频段和关键点 JSON，支持任务回放。
- **多客户端支持**: 同时处理多个 RTMP 流，每个客户端独立队列管理。

## 架构特点

### 三队列设计

- **CA-ReadyQueue**: 从 RTMP 流提取的原始帧，等待 AI 推理，AI服务启动后才会开始捕获
- **CA-RawQueue**: 等待落盘的原始视频
- **CA-ProcessedQueue**: 目标检测后的处理帧（含关键点），用于生成 HLS 段以及JSON数据
- **RT-ProcessedQueue**: 实时推理结果（约 1 秒缓存），用于 WebSocket 推送给前端展示

### 数据流

```text
RTMP 流 → 帧捕获线程 → CA-ReadyQueue → CA-RawQueue & AI 推理 → CA-ProcessedQueue + RT-ProcessedQueue
                                                       ↓                    ↓
                                               HLS 段 + JSON          WebSocket 推送
```



## 项目结构
`app/`: 主应用代码，包括 API 路由和 WebSocket 处理程序。
- `models/`: 包含用于请求和响应验证的 Pydantic 数据结构。
- `routers/`: API 路由定义。
  - `ai.py`: AI 推理服务路由
  - `inspection.py`: 检查流程路由
  - `task.py`: 任务管理路由
- `services/`: 业务逻辑和 AI 模型集成。
  - `ai.py`: 推理管理器和任务架构
  - `ai_models/`: AI 模型实现
    - `detection.py`: 关键点检测
    - `motion.py`: 动作分析
    - `yolo_detection.py`: 内镜弯折检测器
    - `yolo_task.py`: 内镜弯折检测任务
  - `example_custom_task.py`: 自定义任务示例
  - `task.py`: 任务管理和视频追溯
- `test/`: 测试客户端代码，用于上传视频帧和显示推理结果。
- `docs/`: 项目文档
  - `AI_INFERENCE_ARCHITECTURE.md`: 推理架构说明
  - `QUICK_START_CUSTOM_TASK.md`: 自定义任务快速开始
  - `REFACTORING_SUMMARY.md`: 架构重构总结

RTMP 服务独立运行，使用 mediamtx 提供视频流中转功能。配置文件位于 [mediamtx_v1.15.4](mediamtx_v1.15.4) (for Windows Local Test), [mediamtx_v1.15.5_linux_amd64](mediamtx_v1.15.5_linux_amd64)(for Linux Remote Test)。

## Quick Start for app

```powershell
# 创建虚拟环境并激活
py -3.12 -m venv .venv
.\.venv\Scripts\activate

# 安装依赖（包含 ultralytics 用于内镜弯折检测）
pip install -r requirements.txt
```

## AI 推理架构

系统采用可扩展的任务注册架构，支持多种 AI 模型并行或串行执行：

- **关键点检测**: 检测内窥镜清洗过程中的关键点
- **动作分析**: 分析弯曲、浸泡等清洗动作
- **内镜弯折检测**: 使用 YOLOv8 模型检测内镜是否弯折

### 添加自定义推理任务

系统支持快速扩展新的检测任务，只需 3 步：

1. 创建继承 `InferenceTask` 的任务类
2. 实现 `infer()` 和 `visualize()` 方法
3. 在 `ai.py` 中注册任务

详细说明请参考文档：
- [推理架构说明](docs/AI_INFERENCE_ARCHITECTURE.md)
- [自定义任务快速开始](docs/QUICK_START_CUSTOM_TASK.md)
- [架构重构总结](docs/REFACTORING_SUMMARY.md)

## 运行应用

### 本地开发模式（默认）

```powershell
# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 启动服务（仅本地访问）
uvicorn app.main:app --reload
```

API 将可用在 <http://localhost:8000>

### 生产模式（允许外部访问）

```powershell
# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 启动服务（允许外部访问）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

API 将可用在：
- 本地访问: <http://localhost:8000>
- 外部访问: <http://服务器公网IP:8000>

### 环境变量配置

在 `.env` 文件中设置服务器配置：

```env
# 服务器配置
CLEANSIGHT_SERVER_HOST=0.0.0.0  # 允许外部访问
CLEANSIGHT_SERVER_PORT=8000
```

### 安全注意事项

当允许外部访问时，请注意：

1. **防火墙配置**: 只开放必要端口
2. **HTTPS**: 生产环境建议使用HTTPS
3. **认证**: 考虑添加API认证机制
4. **反向代理**: 建议使用nginx等反向代理

## API 文档

运行后，访问 <http://localhost:8000/docs> 查看交互式 HTTP API 文档。

## API 文档

运行后，访问 <http://localhost:8000/docs> 查看交互式 HTTP API 文档。

### HTTP API 接口

#### 路由 `/ai`

##### 1. 查询 AI 服务状态

- **URL**: `GET /ai/status`
- **描述**: 获取所有客户端的队列状态和AI服务运行信息
- **响应**:

  ```json
  {
    "clients": 2,
    "queues": {
      "camera_001": {
        "ca_raw": 15,
        "ca_processed": 120,
        "rt_processed": 30,
        "rtmp_url": "rtmp://192.168.1.100:1935/live/endoscope"
      }
    }
  }
  ```

##### 2. 加载任务

- **URL**: `GET /ai/load_task/{task_id}`
- **描述**: 从数据库加载任务，为指定task_id的任务在AI服务中创建任务对象
- **路径参数**:
  - `task_id` (int): 任务唯一标识符
- **响应**:

  ```json
  {
    "task_id": 0,
    "status": "running",
    "cleaning_stage": "1",
    "bending": false,
    "bubble_detected": false,
    "fully_submerged": false,
    "updated_at": "2024-01-01T12:00:00"
  }
  ```

##### 3. 终止任务

- **URL**: `POST /ai/terminate_task/{client_id}`
- **描述**: 终止指定的清洗任务，清理所有AI服务资源
- **路径参数**:
  - `client_id` (str): 客户端唯一标识符
- **响应**:

  ```json
  {
    "status": "success",
    "message": "Task terminated for client camera_001"
  }
  ```

#### 路由 `/inspection`

##### 1. 启动 RTMP 流捕获

- **URL**: `POST /inspection/start_rtmp_stream`
- **描述**: 启动RTMP流捕获，以固定帧率提取视频帧
- **请求体**:

  ```json
  {
    "client_id": "camera_001",
    "rtmp_url": "rtmp://localhost:1935/live/endoscope",
    "fps": 30
  }
  ```

- **响应**:

  ```json
  {
    "status": "success",
    "message": "RTMP 流捕获已启动 for camera_001"
  }
  ```

##### 2. 停止 RTMP 流捕获

- **URL**: `POST /inspection/stop_rtmp_stream?client_id={client_id}`
- **描述**: 停止指定客户端的RTMP流捕获
- **查询参数**:
  - `client_id` (str): 客户端唯一标识符
- **响应**:

  ```json
  {
    "status": "success",
    "message": "RTMP 流捕获已停止 for camera_001"
  }
  ```

##### 3. 启动 RTSP 流捕获

- **URL**: `POST /inspection/start_rtsp_stream`
- **描述**: 启动 RTSP 流捕获；请求体包含 `client_id`, `rtsp_url`, `fps`。
- **请求体示例**:

  ```json
  {
    "client_id": "camera_001",
    "rtsp_url": "rtsp://localhost:8004/live/stream",
    "fps": 30
  }
  ```
- **响应示例**:

  ```json
  {
    "status": "success",
    "message": "RTSP 流捕获已启动 for camera_001"
  }
  ```

##### 4. 停止 RTSP 流捕获

- **URL**: `POST /inspection/stop_rtsp_stream?client_id={client_id}`
- **描述**: 停止指定客户端的 RTSP 流捕获。
- **查询参数**:
  - `client_id` (str): 客户端唯一标识符
- **响应示例**:

  ```json
  {
    "status": "success",
    "message": "RTSP 流捕获已停止 for camera_001"
  }
  ```

#### 路由 `/task`

##### 1. 获取任务视频段信息

- **URL**: `GET /task/traceback/{task_id}/segments`
- **描述**: 获取任务的所有HLS视频段路径和关键点JSON路径
- **路径参数**:
  - `task_id` (int): 任务ID
- **查询参数**:
  - `video_type` (str, 可选): 视频类型 ("raw" 或 "processed", 默认 "processed")
- **响应**:

  ```json
  {
    "task_id": 0,
    "video_type": "processed",
    "total_segments": 5,
    "playlist_path": "/data/task_0/processed_playlist.m3u8",
    "segments": [
      {
        "segment_id": 1,
        "segment_path": "/data/task_0/processed_segment_1735689600000.mp4",
        "start_time": "2024-01-01T12:00:00",
        "end_time": "2024-01-01T12:00:10",
        "client_id": "camera_001",
        "keypoints_path": "/data/task_0/keypoints_1735689600000.json"
      }
    ]
  }
  ```

##### 2. 获取任务播放列表

- **URL**: `GET /task/traceback/{task_id}/playlist`
- **描述**: 获取任务的HLS播放列表文件(.m3u8)
- **路径参数**:
  - `task_id` (int): 任务ID
- **查询参数**:
  - `video_type` (str, 可选): 视频类型 ("raw" 或 "processed", 默认 "processed")
- **响应**: M3U8播放列表文件

##### 3. 流式传输视频段

- **URL**: `GET /task/traceback/{task_id}/video/{segment_id}`
- **描述**: 流式传输指定的视频段
- **路径参数**:
  - `task_id` (int): 任务ID
  - `segment_id` (int): 段ID
- **响应**: MP4视频文件流

##### 4. 获取关键点数据

- **URL**: `GET /task/traceback/{task_id}/keypoints/{segment_id}`
- **描述**: 获取指定视频段的关键点JSON数据
- **路径参数**:
  - `task_id` (int): 任务ID
  - `segment_id` (int): 段ID
- **响应**: 关键点JSON数据

##### 5. 获取所有关键点数据

- **URL**: `GET /task/traceback/{task_id}/all_keypoints`
- **描述**: 获取任务的所有关键点数据（合并所有段）
- **路径参数**:
  - `task_id` (int): 任务ID
- **响应**:

  ```json
  {
    "task_id": 0,
    "total_frames": 900,
    "keypoints": [
      {
        "frame_id": 1,
        "timestamp": 1735689600000,
        "keypoints": [...],
        "confidence": 0.95
      }
    ]
  }
  ```

##### 6. 获取任务告警记录

- **URL**: `GET /task/{task_id}/alarms`
- **描述**: 查询本地数据库中 `alarm_record` 表为指定 `task_id` 保存的所有告警记录，按 `created_at` 降序返回。适用于回溯某任务的所有异常事件与上报历史。
- **路径参数**:
  - `task_id` (int): 任务ID
- **响应示例**:

```json
{
  "task_id": 1,
  "total": 2,
  "alarms": [
    {
      "id": 123,
      "task_id": 1,
      "step_id": "0",
      "alarm_type": "流程违规",
      "alarm_level": "high",
      "alarm_message": "检测到未按规范操作：操作员未佩戴手套",
      "alarm_time": "2025-12-08T20:30:15",
      "detection_result": {"detected_objects": ["person","glove"], "confidence": 0.95},
      "camera_ip": "192.168.1.64",
      "reader_ip": "172.16.77.221",
      "created_at": "2025-12-08T20:30:20"
    }
  ]
}
```

**cURL 示例**:

```bash
curl -X GET "http://localhost:8000/task/1/alarms"
```

注意：当前实现会在运行时尝试创建并写入 `alarm_record` 表（针对 PostgreSQL）。若使用其他数据库，请确保表结构兼容或采用 ORM/migration 管理表结构。

### WebSocket 接口文档

#### 1. 实时视频流结果推送

- **URL**: `ws://localhost:8000/ai/video?client_id={client_id}`
- **描述**: 实时接收 AI 处理后的视频帧（含关键点标注）
- **连接参数**:
  - `client_id` (必需): 客户端唯一标识符
- **数据格式**: Base64 编码的 JPEG 图像

  ```javascript
  // 接收示例
  data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ...
  ```

#### 2. 任务状态实时更新

- **URL**: `ws://localhost:8000/task/status/{client_id}`
- **描述**: 实时接收任务状态更新
- **路径参数**:
  - `client_id` (必需): 客户端唯一标识符
- **数据格式**: JSON

  ```json
  // 有活跃任务时
  {
    "task_id": "task_123",
    "status": "active",
    "cleaning_stage": 1,
    "bending_count": 5,
    "bubble_detected": false,
    "fully_submerged": true,
    "updated_at": "2024-01-01T12:00:00"
  }

  // 无活跃任务时
  {
    "status": "no_active_task"
  }
  ```

## 使用示例

### 测试脚本

#### 本地完整管道测试

```bash
# 运行完整的本地管道测试（需要本地MediaMTX服务）
# rtmp
python integration_tests/test_full_pipeline.py
# rtsp
python integration_tests/test_full_pipeline_rtsp.py
```

#### 远程服务器测试

用于测试部署在远程服务器上的CleanSight服务：

```bash
# 基本用法
python integration_tests/remote_test_pipeline.py --server 192.168.1.100

# 自定义参数
python integration_tests/remote_test_pipeline.py --server 192.168.1.100 --duration 120 --task_id 0 --client_id remote_test_client
```

远程测试功能：
- 向远程服务器推送RTMP视频流
- 加载远程任务 (task_id=0)
- 实时接收AI推理结果和状态更新
- 本地可视化显示远程处理结果
- 自动化测试报告

详细使用说明见：[远程测试框架文档](integration_tests/REMOTE_TEST_README.md)

## 实时视频流

### 架构

- **捕获线程**: 持续从视频源捕获最新帧。
- **推理线程**: 使用 AI 模型处理帧（当前为模拟实现）。
- **WebSocket 线程**: 将处理结果推送到连接的客户端。
- **帧丢弃**: 自动丢弃旧帧以保持实时性能。

### Websocket推理结果获取接口

- **URL**: `ws://localhost:8000/ai/video?client_id={client_id}`
- **请求类型**: WebSocket
- **描述**: 实时视频流，包含 AI 处理结果。
- **连接参数**:
  - `client_id` (必需): 客户端唯一标识符
- **数据格式**: Base64 编码的 JPEG 图像 (`data:image/jpeg;base64,...`)
