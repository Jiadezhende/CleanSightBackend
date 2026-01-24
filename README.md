# CleanSightBackend

CleanSight 是一个用于长海医院内镜清洗过程 AI 检测的后端系统。它确保每个清洗步骤都正确执行，从而提高患者安全性和合规性。

## 功能简介

- **实时视频流处理**: 捕获视频，使用 AI 模型处理，并通过 WebSocket 推送结果。
- **三服务架构**: 解耦视频流收发、AI 推理和 Client数据管理，优化性能。
- **多任务并行推理**: 支持多种 AI 模型并行执行（关键点检测、动作分析、内镜弯折检测等）。
- **可扩展架构**: 基于任务注册表的设计，便于添加新的检测任务。
- **RTMP 流处理**: 从 RTMP 流以固定帧率提取视频帧，支持实时监控。
- **AI 推理**: 关键点检测 + 动作分析，实时评估清洗过程。
- **实时推送**: 通过 WebSocket 推送处理后的视频帧和推理结果。
- **视频追溯**: 自动生成 HLS 视频段和关键点 JSON，支持任务回放。
- **多客户端支持**: 同时处理多个 RTMP 流，每个客户端独立队列管理。

## 项目结构

`app/`: 主应用代码，包括 API 路由和 WebSocket 处理程序。

- `models/`: 包含用于请求和响应验证的 Pydantic 数据结构。
- `routers/`: API 路由定义。
  - `ai.py`: AI 推理服务路由
  - `inspection.py`: 视频流服务路由
  - `task.py`: 消洗任务管理路由
- `services/`: 业务逻辑和 AI 模型集成。
  - `ai.py`: 推理管理器和任务架构
  - `ai_models/`: AI 模型实现
    - `bubble_detection.py`: 气泡检测器
    - `bubble_task.py`: 气泡检测任务
    - `yolo_detection.py`: 内镜弯折检测器
    - `yolo_task.py`: 内镜弯折检测任务
  - `client.py`: 客户端缓存结构实现
  - `infer_task.py`: 推理任务基类
  - `pipeline_base.py`: 推理流水线基类实现
  - `task.py`: 任务管理和视频追溯
- `test/`: 测试客户端代码，用于上传视频帧和显示推理结果。
- `integration_tests/`: 集成测试与端到端/远程测试脚本（用于验证完整管道）。
- `docs/`: 核心设计文档

## 架构说明

```text
RTMP 流 → 帧捕获线程 → CA-ReadyQueue → CA-RawQueue & AI 推理 → CA-ProcessedQueue + RT-ProcessedQueue
                                                       ↓                    ↓
                                               HLS 段 + JSON          WebSocket 推送
```

### Client数据管理：三队列设计

- **CA-ReadyQueue**: 从 RTMP 流提取的原始帧，等待 AI 推理，AI服务启动后才会开始捕获
- **CA-RawQueue**: 等待落盘的原始视频
- **CA-ProcessedQueue**: 目标检测后的处理帧（含关键点），用于生成 HLS 段以及JSON数据
- **RT-ProcessedQueue**: 实时推理结果（约 1 秒缓存），用于 WebSocket 推送给前端展示

### 独立持久化与帧率控制

系统采用独立持久化策略和解耦的帧率控制机制，以优化性能和资源利用：

#### 独立持久化

- **CA-RawQueue 与 CA-ProcessedQueue 独立落盘**：两个队列以不同速率积累数据并独立触发持久化
  - `CA-RawQueue` 以视频源帧率（通常 30fps）积累原始帧
  - `CA-ProcessedQueue` 以推理流水线吞吐率（约 15fps）积累处理后的帧
  - 每个队列独立检查，达到阈值（默认 300 帧，约 10 秒）时自动触发落盘
  - 持久化操作异步执行，不阻塞实时推理和视频流

#### 帧率控制策略

系统采用统一的 `inference_fps` 参数控制推理、可视化和处理视频输出：

1. **推理帧率** (`inference_fps`)
   - 控制每秒送入推理流水线的帧数（默认 20fps）
   - 通过降频策略减少计算负载，同时保持检测准确性
   - 配置位置：`settings.inference_fps`
   - **影响范围**：
     - 推理采样频率
     - 可视化输出频率
     - 处理视频（processed）的帧率

2. **原始视频帧率**
   - 原始视频（raw）保持视频源帧率（通常 30fps）
   - 完整记录清洗过程，不受推理降频影响

3. **实时显示策略**
   - 使用 `RT-ProcessedQueue`（1 秒循环缓冲区）实现流畅的实时显示
   - 可视化时使用**最新原始帧 + 最近的推理结果**进行渲染
   - 显示帧率跟随推理帧率

#### 设计优势

- **内存优化**：推理降频减少队列积压，避免内存溢出
- **计算优化**：减少不必要的推理计算，提升系统并发能力
- **灵活配置**：根据硬件性能和业务需求独立调节推理频率和视频质量
- **持久化解耦**：两个队列独立落盘，避免因速率差异导致的阻塞

#### 相关配置参数

- `inference_fps`: 推理帧率（默认 20fps）- 统一控制推理、可视化和处理视频的帧率
- `ca_segment_seconds`: 视频段时长（默认 10 秒）
- `ca_segment_len`: 视频段帧数阈值（取决于视频源帧率，默认 300 帧）

**注意**：
- 原始视频（raw）保持30fps，完整记录过程
- 处理视频（processed）使用 `inference_fps` 作为帧率
- 调整 `inference_fps` 可以在性能和质量之间权衡

### RTSP 服务

独立运行，使用 mediamtx 提供视频流中转功能。配置文件位于 [mediamtx_v1.15.4](mediamtx_v1.15.4) (for Windows Local Test), [mediamtx_v1.15.5_linux_amd64](mediamtx_v1.15.5_linux_amd64)(for Linux Remote Test)。

更详细的并发考量，请参考文档：[RTSP 多流处理(Coming Soon)](docs/RTSP_MULTI_STREAM.md)

### AI 推理架构

TODO: 更新 AI 推理架构设计

系统采用可扩展的推理流水线注册架构，在流水线中支持多种 AI 模型并行或串行执行：

将不同推理任务封装为独立的 `InferenceTask` 类，并通过 `SubTaskPipeline` 进行组合和调度。一个`SubTaskPipeline`可以分为以下两个阶段：

- **关键点检测/目标检测**: 检测内窥镜清洗过程中的关键点
- **时序动作分析**: 分析弯曲、浸泡等清洗动作

最后由`TaskPipeline`进行结果聚合和任务状态管理。

详细说明请参考文档：

- [推理与后处理架构设计](docs/INFERENCE_SERVICE_ARCHITECTURE.md)
- [流水线切换逻辑(Coming soon)]()
- [推理任务注册设计(Coming soon)]()

## Quick Start for app

### 环境配置

```powershell
# 创建虚拟环境并激活，使用python3.10+
python -m venv .venv
# source .venv/bin/activate # Linux/Mac
.\.venv\Scripts\activate # Windows

# 安装依赖（可能需要镜像源）
pip install -r requirements.txt
```

***另外，确保安装 ffmpeg 可执行文件，并将其路径添加到系统 PATH 中，用于解码 RTSP 流***

参考 [.env.example](.env.example) 创建 `.env.dev` (开发) 和 `.env` (生产) 配置文件。主要需设定数据库地址、密码，以及模型参数文件路径。

**启动**：
使用 [start_prod.ps1](start_prod.ps1) (Windows) 或 [start_prod.sh](start_prod.sh) (Linux) 启动，通过修改CLEANSIGHT_PROD切换环境

- 开发环境(CLEANSIGHT_PROD=0)：加载 `.env.dev`
- 生产环境(CLEANSIGHT_PROD=1)：加载 `.env`

**手动启动生产环境**:

```powershell
# Windows
.\.venv\Scripts\activate
$env:CLEANSIGHT_PROD = '1'
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Linux/Mac
source .venv/bin/activate
export CLEANSIGHT_PROD=1
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Docker Compose 本地开发环境（开发中）

Docker 化的双服务开发栈（Postgres + MediaMTX）已经编排在 [docker-compose.yml](docker-compose.yml)。

- **启动**：第一次运行会拉取镜像并构建上述服务镜像。

  ```powershell
  # docker构建服务并启动
  docker compose up --build
  # 本地启动后端
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  ```

  启动完成后：
  - API: <http://localhost:8000/docs>
  - Postgres: `postgresql://cleansight:cleansight@localhost:5432/cleansight`
  - MediaMTX: `rtmp://localhost:1935/live/<stream>`、`rtsp://localhost:8004/<path>`

- **组件说明**
  - `db`：`postgres:15-alpine`，持久化卷 `postgres_data` 保存数据文件。
  - `mediamtx`：使用官方 `bluenviron/mediamtx:1.15.4` 镜像，并挂载 [mediamtx_v1.15.4/mediamtx.yml](mediamtx_v1.15.4/mediamtx.yml) 作为配置，可直接在宿主机修改后 `docker compose restart mediamtx` 生效。
  - **（开发中）** 完全采用python代码访问数据库，而非使用脚本。

- **常用命令**
  - 停止并移除资源：`docker compose down -v`
  - 查看应用日志：`docker compose logs -f app`
  - 进入数据库：`docker compose exec db psql -U cleansight -d cleansight`

> 提示：如果需要变更数据库凭据或端口，可直接编辑 [docker-compose.yml](docker-compose.yml)，同时更新 `app` 服务的 `CLEANSIGHT_*` 变量即可。

## Quick Start for mediamtx

本项目使用 MediaMTX ，用于 RTSP 流的中转和分发。

根据运行平台，`cd`到对应目录`mediamtx_*/`，运行 MediaMTX 可执行文件即可。

## 测试

### 本地完整管道测试

```bash
# 运行完整的本地管道测试（需要本地MediaMTX和后端服务）
# rtsp
python integration_tests/local_full_pipeline_rtsp.py -duration 30 --task_id 1
```

### 远程服务器测试

用于测试部署在远程服务器上的CleanSight服务：

```bash
python integration_tests/remote_full_pipeline_rtsp.py --duration 120 --task_id 1 --server 36.103.203.206
```

### 参数说明

- `--task_id`: 要测试的任务 ID（默认: 0）
- `--client_id`: 客户端标识符，最好是和任务的source_id一致
- `--duration`: 测试时长秒数（默认: 30）
- `--video_path`: 测试视频路径（默认: test/test_video.mp4）
- `--no-window`: 禁用可视化窗口
- `--server`: 远程服务器地址

远程测试功能：

- 向远程服务器推送RTSP视频流
- 加载远程任务 (task_id=1)
- 实时接收AI推理结果和状态更新
- 本地可视化显示远程处理结果
- 自动化测试报告

详细使用说明见：[RTSP测试流说明](docs/RTSP_FLOW.md)

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
