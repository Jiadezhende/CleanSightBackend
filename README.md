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
py -3.12 -m venv .venv
.\.venv\Scripts\activate
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

### 多客户端并发测试 (推荐 — WebSocket)

仓库自带一个用于模拟大量摄像头和展示端的测试脚本：`test/multi_client.py`。
该脚本能并发启动若干上传客户端（camera clients）与可选的展示客户端（display clients），
便于在本地进行端到端连通性与路由验证（适合模拟医院级别的多设备场景）。

默认行为与设计要点：

- 默认以 **WebSocket** 模式运行（推荐用于性能与实时性测试）。
- 上传端只保留并发送包含客户标记（左上角小方块）的帧，用于结果回传验证。
- 展示端（若启用 `--display`）会接收推理后图像并验证左上角标记颜色是否保留，从而判断结果是否路由到对应 `client_id`。
- 默认**不保存**接收的 output（节约磁盘）。如需保存，可使用 `--display --save-frames --output-dir <dir>`。

依赖（如未安装）：

```powershell
pip install aiohttp websockets opencv-python numpy
```

用法摘要（PowerShell）：

- 在激活虚拟环境且启动后端后（见上文）进入 `test` 目录：

```powershell
cd test
```

- 启动默认的 WebSocket 多客户端测试（10 个客户端，上传间隔 0.5s，不保存 output）：

```powershell
py .\multi_client.py --num 10 --mode websocket --frame test_frame.jpg --send-interval 0.5 --server-ws ws://127.0.0.1:8000
```

- 启动并启用展示端以进行验证（每个客户端都启动一个 display client，会显著增加本地连接数）：

```powershell
py .\multi_client.py --num 10 --mode websocket --frame test_frame.jpg --send-interval 0.5 --display --server-ws ws://127.0.0.1:8000
```

- 将展示端同时保存接收帧到目录：

```powershell
py .\multi_client.py --num 10 --mode websocket --frame test_frame.jpg --send-interval 0.5 --display --save-frames --output-dir multi_output --server-ws ws://127.0.0.1:8000
```

主要可选参数说明（摘录）：

- `--num`: 并发客户端数量，默认 `10`。
- `--mode`: `http` 或 `websocket`，默认 `websocket`（推荐）。
- `--frame`: 用作上传的静态帧文件路径（脚本会在每个 client 的帧左上角画上颜色标记），默认 `test_frame.jpg`。
- `--send-interval`: 每个客户端发送帧的间隔（秒），默认 `0.5`。
- `--server-ws`: 服务的 WS 地址，默认 `ws://127.0.0.1:8000`。
- `--display`: 启用每个客户端对应的展示/验证连接（默认关闭）。
- `--save-frames`: 启用保存接收到的帧（需要 `--display`），默认关闭。
- `--output-dir`: 保存接收帧的目录（若启用 `--save-frames`），默认 `multi_output`。

诊断提示：

- 如果运行后统计显示 `clients=0, ok=0`，一般是因为未启用 `--display`（上传端不会填充 stats），或 display 端无法连接到 `/ai/video`（检查 `--server-ws` 地址与防火墙）。
- 仅上传（不启用 `--display`）时，脚本用于压测上传通道与服务器接收能力；验证需要 `--display`。

## 单客户端测试说明

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
