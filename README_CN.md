# CleanSightBackend

CleanSight 是一个用于长海医院内镜清洗过程 AI 检测的后端系统。它确保每个清洗步骤都正确执行，从而提高患者安全性和合规性。

## 功能简介

- **实时视频流处理**: 从摄像头或本地文件捕获视频，使用 AI 模型处理，并通过 WebSocket 推送结果。
- **三线程架构**: 解耦帧捕获、AI 推理和 WebSocket 推送，优化性能。

## 项目结构

- `models/`: 包含用于请求和响应验证的 Pydantic 数据结构。
- `app/`: 主应用代码，包括 API 路由和 WebSocket 处理程序。
- `routers/`: API 路由定义。
- `services/`: 业务逻辑和 AI 模型集成。
- `test/`: 测试客户端代码，用于上传视频帧和显示推理结果。

## 安装

```powershell
# 创建虚拟环境并激活
py -3.11 -m venv .\venv
.\venv\Scripts\activate
# 安装依赖
pip install -r requirements.txt
```

## 运行应用

在激活的虚拟环境中运行：

```powershell
uvicorn app.main:app
```

API 将可用在 <http://localhost:8000>

## API 文档

运行后，访问 <http://localhost:8000/docs> 查看交互式 API 文档。

## 实时视频流

### 架构

- **捕获线程**: 持续从视频源捕获最新帧。
- **推理线程**: 使用 AI 模型处理帧（当前为模拟实现）。
- **WebSocket 线程**: 将处理结果推送到连接的客户端。
- **帧丢弃**: 自动丢弃旧帧以保持实时性能。

### Websocket推理结果获取接口

- **URL**: `ws://localhost:8000/ai/video`
- **请求类型**: WebSocket
- **描述**: 实时视频流，包含 AI 处理结果。
- **数据格式**: Base64 编码的 JPEG 图像 (`data:image/jpeg;base64,...`)

### Http视频帧上传接口

- **URL**: `http://localhost:8000/inspection/upload_frame`
- **描述**: 接收来自网络的 Base64 编码视频帧进行处理。
- **请求类型**: POST

### Websocket视频帧上传接口

- **URL**: `ws://localhost:8000/inspection/upload_stream`
- **请求类型**: WebSocket
- **描述**: 接收来自网络的 Base64 编码视频帧进行处理

## 测试说明

测试文件在 `test/` 目录下：

- **测试客户端**:
  - `upload_client.py`: 支持上传静态帧、视频文件或摄像头流的模式，支持 HTTP 和 WebSocket 传输。
  - `video_client.py`: 显示推理结果，支持自适应窗口和实时帧率。

### 测试方法

1. **启动后端**:

   ```powershell
   uvicorn app.main:app --reload
   ```

2. **测试 WebSocket 推流**:
   - 进入 `test/` 目录：`cd test`
   - 运行 `py video_client.py` 连接到 `ws://localhost:8000/ai/video` 并显示结果。

3. **测试帧上传**:
   - 进入 `test/` 目录：`cd test`
   - 运行 `py upload_client.py --mode frame` 用于静态帧（HTTP）。
   - 运行 `py upload_client.py --mode video --video [file_path] --transport http` 用于视频文件上传（HTTP）。
   - 运行 `py upload_client.py --mode video --video [file_path] --transport websocket` 用于视频文件上传（WebSocket）。
   - 运行 `py upload_client.py --mode camera --source 0 --transport http` 用于摄像头流（HTTP）。
   - 运行 `py upload_client.py --mode camera --source 0 --transport websocket` 用于摄像头流（WebSocket）。
   - 帧以 ~30 FPS 上传、处理并通过 WebSocket 推送。
