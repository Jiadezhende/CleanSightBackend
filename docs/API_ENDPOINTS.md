# CleanSight API 端点文档

本文档详细说明了 CleanSight 后端提供的所有 API 接口，包括请求格式、参数说明、响应示例和使用方法。

## 概述

CleanSight 后端提供 RESTful API 和 WebSocket 接口，用于管理内镜清洗任务、实时视频流推理和历史数据回溯。

### 基础信息

- **基础URL**: `http://localhost:8000` (开发环境)
- **API文档**: `http://localhost:8000/docs` (Swagger UI)
- **协议**: HTTP/1.1 和 WebSocket

### 版本说明

**当前版本为过渡版本**，对部分 API 功能进行了合并和重构：

- **推荐使用**: `/api` 统一接口和 `/health` 健康监控接口
- **过渡保留**: `/ai` 和 `/inspection` 的部分接口仍可用，但标记为 **⚠️ 过渡接口**
- **未来计划**: 过渡接口将在后续版本中移除，请尽快迁移到新接口

---

## 错误响应规范

CleanSight 后端采用**统一的异常处理架构**，所有错误响应都遵循以下格式。

### HTTP 状态码

| 状态码 | 说明 | 使用场景 |
|--------|------|---------|
| **400 Bad Request** | 客户端请求参数错误 | 参数验证失败、必填字段缺失 |
| **404 Not Found** | 资源不存在 | 任务/视频段/播放列表不存在 |
| **409 Conflict** | 资源冲突 | 流已在运行、任务已存在 |
| **500 Internal Server Error** | 服务器内部错误 | 模型推理失败、设置任务失败 |
| **503 Service Unavailable** | 服务暂时不可用 | 数据库连接失败、RTSP 连接失败 |

### 统一错误响应格式

所有错误响应都包含以下字段：

```json
{
  "error": "错误类型",
  "detail": "详细错误信息",
  "field": "验证失败的字段（仅 400）",
  "resource_type": "资源类型（仅 404/409）",
  "resource_id": "资源ID（仅 404/409）",
  "retryable": true/false,
  "client_id": "客户端ID（如果相关）"
}
```

### 错误类型与示例

#### 1. 参数验证错误（400）

**场景**: 请求参数不合法、必填字段缺失、参数范围错误

```json
{
  "error": "Validation error",
  "detail": "Task source_ip is required",
  "field": "source_ip"
}
```

**常见原因**:
- `source_ip` 为空
- `rtsp_url` 格式错误
- 必填字段未提供

**处理建议**: 检查请求参数，确保所有必填字段都已提供且格式正确

---

#### 2. 资源不存在错误（404）

**场景**: 查询的任务、视频段、播放列表不存在

```json
{
  "error": "Resource not found",
  "detail": "Task 99999 not found",
  "resource_type": "Task",
  "resource_id": "99999"
}
```

**常见原因**:
- 任务ID不存在于数据库
- 视频段已被删除
- 播放列表未生成

**处理建议**: 确认资源ID是否正确，检查资源是否已被删除

---

#### 3. 资源冲突错误（409）

**场景**: 试图创建或修改已存在的资源，产生冲突

```json
{
  "error": "Resource conflict",
  "detail": "Stream already running for client 192.168.1.100. Stop it first to change stream URL.",
  "client_id": "192.168.1.100",
  "resource_type": "Stream",
  "resource_id": "192.168.1.100"
}
```

**常见原因**:
- 流已经在运行，无法重复启动
- 任务已存在，无法重复创建
- 资源状态冲突（如试图停止未启动的流）

**处理建议**: 先停止现有资源，然后再创建新资源。或使用 `GET` 查询接口确认资源状态

---

#### 4. 服务不可用错误（503）

**场景**: 外部服务暂时不可用（数据库、RTSP 流等）

##### 数据库错误

```json
{
  "error": "Database unavailable",
  "detail": "Database error: Failed to query task 1",
  "retryable": true
}
```

##### RTSP 连接错误

```json
{
  "error": "Stream unavailable",
  "detail": "Stream connection failed: rtsp://localhost:8554/live/stream - Connection timeout",
  "client_id": "192.168.1.100"
}
```

**常见原因**:
- 数据库连接失败或连接池耗尽
- RTSP 流地址不可达
- 网络瞬时故障

**处理建议**:
- 查看 `retryable` 字段，如果为 `true` 可以重试
- 检查外部服务状态（数据库、MediaMTX）
- 等待一段时间后重试

---

#### 5. 服务器内部错误（500）

**场景**: 服务器内部逻辑错误、模型推理失败等

##### 模型推理错误

```json
{
  "error": "Inference failed",
  "detail": "Model inference error: CUDA out of memory (model=yolov8_bubble)",
  "client_id": "192.168.1.100"
}
```

##### FFmpeg 解码错误

```json
{
  "error": "FFmpeg error",
  "detail": "FFmpeg error: Failed to launch FFmpeg (exit_code=1)",
  "client_id": "192.168.1.100"
}
```

##### 持久化错误

```json
{
  "error": "Persistence failed",
  "detail": "Persistence error: Failed to create directory: /data/videos (operation=hls_mkdir)",
  "retryable": true
}
```

##### 通用内部错误

```json
{
  "error": "Internal error",
  "detail": "Failed to set task for client 192.168.1.100",
  "retryable": false
}
```

##### 兜底错误（敏感信息已隐藏）

```json
{
  "error": "Internal server error",
  "detail": "An unexpected error occurred. Please contact support if the issue persists."
}
```

**常见原因**:
- GPU 内存不足
- FFmpeg 解码失败
- 文件系统错误
- 业务逻辑异常

**处理建议**:
- 查看 `retryable` 字段决定是否重试
- 检查服务器日志获取详细堆栈信息
- 对于 CUDA OOM，可能需要减少并发任务数
- 对于持久化错误，检查磁盘空间

---

### 错误响应处理最佳实践

#### Python 示例

```python
import requests

def start_task(task_id, rtsp_url):
    try:
        response = requests.post(
            "http://localhost:8000/api/start",
            json={"task_id": task_id, "rtsp_url": rtsp_url}
        )
        response.raise_for_status()
        return response.json()

    except requests.HTTPError as e:
        error_data = e.response.json()

        if e.response.status_code == 404:
            # 资源不存在
            print(f"任务不存在: {error_data['detail']}")
            print(f"资源类型: {error_data.get('resource_type')}")
            print(f"资源ID: {error_data.get('resource_id')}")

        elif e.response.status_code == 400:
            # 参数验证错误
            print(f"参数错误: {error_data['detail']}")
            print(f"字段: {error_data.get('field')}")

        elif e.response.status_code == 503:
            # 服务不可用
            print(f"服务不可用: {error_data['detail']}")
            if error_data.get('retryable'):
                print("建议: 稍后重试")
                # 实现重试逻辑

        elif e.response.status_code == 500:
            # 内部错误
            print(f"内部错误: {error_data['detail']}")
            if error_data.get('retryable'):
                print("建议: 可以重试")
            else:
                print("建议: 检查服务器日志")
```

#### JavaScript 示例

```javascript
async function startTask(taskId, rtspUrl) {
  try {
    const response = await fetch('http://localhost:8000/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, rtsp_url: rtspUrl })
    });

    if (!response.ok) {
      const errorData = await response.json();

      switch (response.status) {
        case 404:
          console.error('资源不存在:', errorData.detail);
          console.error('资源类型:', errorData.resource_type);
          break;

        case 400:
          console.error('参数错误:', errorData.detail);
          console.error('字段:', errorData.field);
          break;

        case 503:
          console.error('服务不可用:', errorData.detail);
          if (errorData.retryable) {
            console.log('建议: 稍后重试');
            // 实现重试逻辑
          }
          break;

        case 500:
          console.error('内部错误:', errorData.detail);
          if (errorData.retryable) {
            console.log('建议: 可以重试');
          }
          break;
      }

      throw new Error(errorData.detail);
    }

    return await response.json();

  } catch (error) {
    console.error('请求失败:', error);
    throw error;
  }
}
```

---

## 零、统一 API (`/api`) - **推荐使用**

统一 API 提供简化的任务管理接口，合并了原有的多步操作，简化了使用流程。

### 0.1 启动任务和流

- **URL**: `POST /api/start`
- **描述**: 统一的启动接口，合并了 `load_task` + `start_rtsp_stream` 两步操作。

#### 请求体

```json
{
  "task_id": 1,
  "rtsp_url": "rtsp://localhost:8554/live/stream",
  "fps": 30
}
```

**字段说明**:
- `task_id` (integer, 必填): 任务ID，必须在 clean_task 表中存在
- `rtsp_url` (string, 必填): RTSP 流地址
- `fps` (integer, 可选): 目标帧率，默认 30

#### 成功响应示例 (200 OK)

```json
{
  "status": "success",
  "client_id": "192.168.1.100",
  "task_id": 1,
  "rtsp_url": "rtsp://localhost:8554/live/stream",
  "message": "Task 1 started for client 192.168.1.100"
}
```

#### 错误响应

**404 Not Found** - 任务不存在
```json
{
  "error": "Resource not found",
  "detail": "Task 1 not found",
  "resource_type": "Task",
  "resource_id": "1"
}
```

**400 Bad Request** - source_ip 为空
```json
{
  "error": "Validation error",
  "detail": "Task source_ip is required",
  "field": "source_ip"
}
```

**500 Internal Server Error** - 启动失败
```json
{
  "error": "Internal error",
  "detail": "Failed to set task for client 192.168.1.100",
  "retryable": false
}
```

**503 Service Unavailable** - 数据库不可用
```json
{
  "error": "Database unavailable",
  "detail": "Database error: Failed to query task 1",
  "retryable": true
}
```

**503 Service Unavailable** - RTSP 连接失败
```json
{
  "error": "Stream unavailable",
  "detail": "Stream connection failed: rtsp://localhost:8554/live/stream",
  "client_id": "192.168.1.100"
}
```

#### cURL 示例

```bash
curl -X POST "http://localhost:8000/api/start" \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": 1,
    "rtsp_url": "rtsp://localhost:8554/live/stream",
    "fps": 30
  }'
```

#### 注意事项

- 自动检测跨任务切换并清理旧数据
- 从数据库读取任务信息，使用 `source_ip` 作为 `client_id`
- 一次调用完成任务加载和流启动
- 启动后可通过 WebSocket `/ai/video` 接收推理结果

---

### 0.2 终止任务

- **URL**: `POST /api/terminate?client_id=<client_id>`
- **描述**: 统一的终止接口，执行完整的三步清理流程。

#### 查询参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| client_id | string | 是 | 客户端ID（通常是 source_ip） |

#### 成功响应示例 (200 OK)

```json
{
  "status": "success",
  "client_id": "192.168.1.100",
  "reason": "API termination request",
  "decoder_stopped": true,
  "data_flushed": true,
  "client_cleaned": true,
  "errors": []
}
```

**字段说明**:
- `status`: "success" 或 "partial_success"
- `decoder_stopped`: 解码器是否已停止
- `data_flushed`: 数据是否已落盘
- `client_cleaned`: ClientManager 是否已清理
- `errors`: 错误列表（如果有）

#### cURL 示例

```bash
curl -X POST "http://localhost:8000/api/terminate?client_id=192.168.1.100"
```

#### 注意事项

- 采用"尽力而为"策略，即使某步骤失败也会继续执行
- 执行完整的三步清理：停止解码器 → 落盘数据 → 清理客户端
- 永不抛出异常，总是返回结果
- 由 GlobalHealthMonitor 协调清理流程

---

## 一、健康监控 (`/health`)

健康监控服务提供系统状态查询和监控统计功能，替代原有的 `/ai/status` 接口。

### 1.1 获取系统整体状态

- **URL**: `GET /health/status`
- **描述**: 获取系统整体状态，包括客户端统计、队列状态和监控统计。

#### 请求参数

无

#### 成功响应示例 (200 OK)

```json
{
  "status": "running",
  "clients": {
    "total": 2,
    "active_streams": 2,
    "reconnecting": 0,
    "orphans": 0
  },
  "queues": {
    "192.168.1.100": {
      "raw_queue_size": 30,
      "ready_queue_size": 20,
      "latest_timestamp": 1706599200.5
    },
    "192.168.1.101": {
      "raw_queue_size": 28,
      "ready_queue_size": 18,
      "latest_timestamp": 1706599199.8
    }
  },
  "monitor_stats": {
    "checks": 120,
    "suspects": 2,
    "cleanups": 1,
    "reconnects": 1,
    "reconnect_successes": 1,
    "orphans_detected": 0
  }
}
```

**字段说明**:
- `clients`: 客户端统计信息
  - `total`: 总客户端数（有队列的）
  - `active_streams`: 活跃流数量（有解码器的）
  - `reconnecting`: 重连中的客户端数量
  - `orphans`: 孤儿流数量（有队列但无解码器）
- `queues`: 各客户端的队列状态详情
- `monitor_stats`: 监控统计信息

#### cURL 示例

```bash
curl -X GET "http://localhost:8000/health/status"
```

#### 注意事项

- 推荐使用此接口代替 `/ai/status`
- 提供更完整的系统级状态视图
- 整合来自多个模块的信息

---

### 1.2 获取健康监控统计

- **URL**: `GET /health/monitor/stats`
- **描述**: 获取健康监控的详细统计信息。

#### 成功响应示例 (200 OK)

```json
{
  "status": "running",
  "checks": 120,
  "suspects": 2,
  "cleanups": 1,
  "reconnects": 1,
  "reconnect_successes": 1,
  "orphans_detected": 0,
  "reconnecting_count": 0,
  "reconnecting_clients": []
}
```

**字段说明**:
- `checks`: 检查次数
- `suspects`: 检测到的可疑断流次数
- `cleanups`: 完整清理次数
- `reconnects`: 重连尝试次数
- `reconnect_successes`: 重连成功次数
- `orphans_detected`: 孤儿流检测次数
- `reconnecting_count`: 当前重连中的客户端数量
- `reconnecting_clients`: 当前重连中的客户端ID列表

#### cURL 示例

```bash
curl -X GET "http://localhost:8000/health/monitor/stats"
```

---

### 1.3 获取健康监控配置

- **URL**: `GET /health/monitor/config`
- **描述**: 获取健康监控的配置参数。

#### 成功响应示例 (200 OK)

```json
{
  "status": "running",
  "config": {
    "check_interval": 5,
    "heartbeat_timeout": 10,
    "reconnect_interval": 3,
    "max_reconnect_attempts": 5,
    "orphan_timeout": 90
  },
  "derived": {
    "suspect_timeout": 10,
    "cleanup_timeout": 25
  }
}
```

**字段说明**:
- `config`: 配置参数
  - `check_interval`: 检查间隔（秒）
  - `heartbeat_timeout`: 心跳超时（秒）
  - `reconnect_interval`: 重连延迟（秒）
  - `max_reconnect_attempts`: 最大重连次数
  - `orphan_timeout`: 孤儿流超时（秒）
- `derived`: 派生参数
  - `suspect_timeout`: 可疑断流超时
  - `cleanup_timeout`: 清理超时

#### cURL 示例

```bash
curl -X GET "http://localhost:8000/health/monitor/config"
```

---

## 三、AI 推理服务 (`/ai`) - **⚠️ 过渡接口**

AI 推理服务负责管理视频流的实时分析、任务加载和推理结果推送。

**注意**: 本节中的部分接口已标记为 **⚠️ 过渡接口**，建议迁移到统一 API (`/api`) 和健康监控 API (`/health`)。

### 3.1 获取 AI 服务状态 - **⚠️ 过渡接口**

- **URL**: `GET /ai/status`
- **描述**: 查询 AI 服务当前状态，返回详细的队列信息和运行状态。
- **⚠️ 迁移建议**: 请使用 `GET /health/status` 获取更完整的系统状态

#### 请求参数

无

#### 成功响应示例 (200 OK)

```json
{
  "队列信息": "详细的 AI 服务队列状态"
}
```

#### cURL 示例

```bash
curl -X GET "http://localhost:8000/ai/status"
```

#### 注意事项

- 此接口用于监控和调试 AI 服务状态
- 返回的队列信息包括各客户端的队列长度、处理速度等

---

### 3.2 加载任务 - **⚠️ 过渡接口**

- **URL**: `GET /ai/load_task/{task_id}`
- **描述**: 从数据库加载指定任务，在 AI 服务中创建任务对象，为任务初始化推理管道和状态跟踪。
- **⚠️ 迁移建议**: 请使用 `POST /api/start` 统一启动接口（自动加载任务并启动流）

#### 路径参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| task_id | integer | 是 | 任务ID，必须在 clean_task 表中存在 |

#### 成功响应示例 (200 OK)

```json
{
  "task_id": 1,
  "status": "running",
  "cleaning_stage": "0",
  "bending": false,
  "bubble_detected": false,
  "fully_submerged": false,
  "updated_at": "2025-12-08T20:30:00"
}
```

**字段说明**:
- `task_id`: 任务ID
- `status`: 任务状态（running, paused, completed, cancelled）
- `cleaning_stage`: 当前清洗阶段（0-7）
- `bending`: 是否检测到内镜弯折
- `bubble_detected`: 是否检测到气泡（漏气）
- `fully_submerged`: 是否完全浸没
- `updated_at`: 最后更新时间（ISO 8601格式）

#### 错误响应

**404 Not Found** - 任务不存在
```json
{
  "error": "Resource not found",
  "detail": "Task 1 not found",
  "resource_type": "Task",
  "resource_id": "1"
}
```

**400 Bad Request** - source_ip 为空
```json
{
  "error": "Validation error",
  "detail": "Task source_ip is empty",
  "field": "source_ip"
}
```

**500 Internal Server Error** - 任务加载失败
```json
{
  "error": "Internal error",
  "detail": "Failed to set task for client",
  "retryable": false
}
```

**503 Service Unavailable** - 数据库不可用
```json
{
  "error": "Database unavailable",
  "detail": "Database error: Failed to query task 1",
  "retryable": true
}
```

#### cURL 示例

```bash
# 加载任务 ID 为 1 的任务
curl -X GET "http://localhost:8000/ai/load_task/1"
```

#### 注意事项

- 加载任务前，确保任务已在数据库 `clean_task` 表中创建
- `source_ip` 字段不能为空，系统使用它作为 `client_id`
- 同一 `client_id` 重复加载任务会覆盖之前的任务对象
- 任务加载成功后，需要调用 `/inspection/start_rtsp_stream` 启动视频流

---

### 3.3 终止任务 - **⚠️ 过渡接口**

- **URL**: `POST /ai/terminate_task/{client_id}`
- **描述**: 清理指定客户端的所有 AI 服务资源（队列、任务对象等）。
- **⚠️ 迁移建议**: 请使用 `POST /api/terminate` 统一终止接口（执行完整清理）
- **⚠️ 注意**: 此接口只清理推理资源，不停止流解码器，不清理 ClientManager

#### 路径参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| client_id | string | 是 | 客户端ID（通常是 source_ip） |

#### 成功响应示例 (200 OK)

```json
{
  "status": "success",
  "message": "Task terminated for client 192.168.1.100"
}
```

#### 错误响应

**注意**: 此接口采用"尽力而为"策略，通常不会抛出异常。如果部分步骤失败，会在 `errors` 字段中返回错误信息，但仍返回 200 状态码。

#### cURL 示例

```bash
# 终止 client_id 为 192.168.1.100 的任务
curl -X POST "http://localhost:8000/ai/terminate_task/192.168.1.100"
```

#### 注意事项

- 终止任务会清理所有相关的队列和内存资源
- 建议在停止视频流后调用此接口进行完整清理

---

### 3.4 实时推理结果流（WebSocket）

- **URL**: `ws://<host>/ai/video?client_id=<client_id>`
- **描述**: 通过 WebSocket 实时推送 AI 处理后的视频帧（Base64 编码的 JPEG 图像）。
- **状态**: ✅ 活跃接口，继续使用

#### 查询参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| client_id | string | 是 | 客户端标识符，必须与任务的 source_ip 一致 |

#### 推送数据格式

服务器持续推送 Base64 编码的 JPEG 图像：

```
data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/...
```

#### 连接特性

- **帧率**: 约 30fps（自动帧率控制）
- **去重**: 自动跳过重复帧
- **心跳**: 服务器持续推送，客户端无需发送心跳
- **连接管理**: 支持多客户端并发连接

#### JavaScript 示例

```javascript
const client_id = "192.168.1.100";
const ws = new WebSocket(`ws://localhost:8000/ai/video?client_id=${client_id}`);

ws.onopen = () => {
  console.log('WebSocket 连接已建立');
};

ws.onmessage = (event) => {
  // 接收 Base64 编码的图像数据
  const dataUrl = event.data;  // data:image/jpeg;base64,...

  // 显示在 img 元素中
  const img = document.getElementById('video-frame');
  img.src = dataUrl;
};

ws.onerror = (error) => {
  console.error('WebSocket 错误:', error);
};

ws.onclose = () => {
  console.log('WebSocket 连接已关闭');
};
```

#### Python 示例

```python
import websocket
import base64
from PIL import Image
import io

def on_message(ws, message):
    # 解析 data URL
    if message.startswith("data:image/jpeg;base64,"):
        base64_data = message.split(",")[1]
        image_data = base64.b64decode(base64_data)

        # 转换为图像
        image = Image.open(io.BytesIO(image_data))
        image.show()

def on_error(ws, error):
    print(f"WebSocket 错误: {error}")

def on_close(ws):
    print("WebSocket 连接已关闭")

def on_open(ws):
    print("WebSocket 连接已建立")

# 连接 WebSocket
client_id = "192.168.1.100"
ws_url = f"ws://localhost:8000/ai/video?client_id={client_id}"
ws = websocket.WebSocketApp(ws_url,
                           on_message=on_message,
                           on_error=on_error,
                           on_close=on_close,
                           on_open=on_open)
ws.run_forever()
```

#### 错误响应

**1008 Policy Violation** - 缺少 client_id 参数

#### 注意事项

- 必须先调用 `/ai/load_task/{task_id}` 加载任务
- 必须先调用 `/inspection/start_rtsp_stream` 启动视频流
- WebSocket 会自动处理帧率控制和去重
- 客户端断开连接时，服务器会自动清理资源

---

## 四、视频流管理 (`/inspection`) - **⚠️ 过渡接口**

视频流管理服务负责启动和停止 RTSP 视频流的捕获和解码。

**注意**: 本节中的接口已标记为 **⚠️ 过渡接口**，建议迁移到统一 API (`/api`)。

### 4.1 启动 RTSP 流 - **⚠️ 过渡接口**

- **URL**: `POST /inspection/start_rtsp_stream`
- **描述**: 启动 RTSP 流捕获和 AI 推理。
- **⚠️ 迁移建议**: 请使用 `POST /api/start` 统一启动接口（自动加载任务并启动流）
- **⚠️ 注意**: 此接口只启动流，不加载任务，需要单独调用 `/ai/load_task`

#### 请求体

```json
{
  "client_id": "192.168.1.100",
  "rtsp_url": "rtsp://localhost:8554/live/stream",
  "fps": 30
}
```

**字段说明**:
- `client_id` (string, 必填): 客户端ID，通常为摄像机 IP 或任务的 source_ip
- `rtsp_url` (string, 必填): RTSP 流地址
- `fps` (integer, 可选): 目标帧率，默认 30

#### 成功响应示例 (200 OK)

```json
{
  "status": "success",
  "message": "RTSP 流捕获已启动 for 192.168.1.100"
}
```

#### 错误响应

**503 Service Unavailable** - RTSP 连接失败
```json
{
  "error": "Stream unavailable",
  "detail": "Stream connection failed: rtsp://localhost:8554/live/stream - Connection timeout",
  "client_id": "192.168.1.100"
}
```

**500 Internal Server Error** - FFmpeg 启动失败
```json
{
  "error": "FFmpeg error",
  "detail": "FFmpeg error: Failed to launch FFmpeg (exit_code=1)",
  "client_id": "192.168.1.100"
}
```

#### cURL 示例

```bash
curl -X POST "http://localhost:8000/inspection/start_rtsp_stream" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "192.168.1.100",
    "rtsp_url": "rtsp://localhost:8554/live/stream",
    "fps": 30
  }'
```

#### 注意事项

- 确保 MediaMTX 服务正在运行（端口 8554）
- 确保已调用 `/ai/load_task/{task_id}` 加载任务
- `client_id` 应与任务的 `source_ip` 一致
- 流启动后，可通过 WebSocket `/ai/video` 接收推理结果

---

### 4.2 停止 RTSP 流 - **⚠️ 过渡接口**

- **URL**: `POST /inspection/stop_rtsp_stream?client_id=<client_id>`
- **描述**: 停止 RTSP 流捕获，采用尽力清理模式，即使解码器异常也会清理所有相关资源。
- **⚠️ 迁移建议**: 请使用 `POST /api/terminate` 统一终止接口（执行完整清理）

#### 查询参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| client_id | string | 是 | 客户端ID |

#### 成功响应示例 (200 OK)

```json
{
  "status": "success",
  "message": "RTSP 流捕获已停止 for 192.168.1.100",
  "cleanup_details": {
    "stream_stopped": true,
    "ai_cleaned": true,
    "queues_cleared": true
  }
}
```

#### cURL 示例

```bash
curl -X POST "http://localhost:8000/inspection/stop_rtsp_stream?client_id=192.168.1.100"
```

#### 注意事项

- 采用尽力清理模式，永不失败
- 即使流已经断开或解码器已死，也会清理相关资源
- 建议在停止流后调用 `/ai/terminate_task/{client_id}` 进行完整清理

---

## 五、任务管理 (`/task`)

任务管理服务提供任务状态监控和历史视频回溯功能。

### 5.1 任务状态实时更新（WebSocket）

- **URL**: `ws://<host>/task/status/{client_id}`
- **描述**: 每秒推送一次任务状态信息，包括清洗阶段、检测结果和告警信息。

#### 路径参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| client_id | string | 是 | 客户端ID（摄像机 IP 或 source_ip） |

#### 推送数据格式

**有活跃任务时**:

```json
{
  "task_id": 123,
  "status": {
    "code": "running",
    "text": "运行中",
    "message": "清洗任务正在执行",
    "severity": "success"
  },
  "cleaning_step": {
    "code": "1",
    "name": "预冲洗"
  },
  "detection": {
    "bending": false,
    "bubble_detected": false,
    "fully_submerged": true
  },
  "messages": ["设备运行正常"],
  "updated_at": "2024-01-30T12:00:00"
}
```

**无活跃任务时**:

```json
{
  "task_id": null,
  "status": {
    "code": "idle",
    "text": "空闲",
    "message": "当前无活跃任务",
    "severity": "info"
  },
  "cleaning_step": null,
  "detection": null,
  "messages": ["等待任务启动"],
  "updated_at": null
}
```

#### JavaScript 示例

```javascript
const client_id = "192.168.1.100";
const ws = new WebSocket(`ws://localhost:8000/task/status/${client_id}`);

ws.onmessage = (event) => {
  const statusData = JSON.parse(event.data);
  console.log('任务状态:', statusData);

  if (statusData.task_id) {
    console.log(`任务 ${statusData.task_id}:`);
    console.log(`状态: ${statusData.status.text}`);
    console.log(`清洗阶段: ${statusData.cleaning_step.name}`);
    console.log(`弯折: ${statusData.detection.bending}`);
    console.log(`气泡: ${statusData.detection.bubble_detected}`);
  } else {
    console.log('当前无活跃任务');
  }
};
```

#### 注意事项

- 状态更新频率为 1 秒/次
- 清洗阶段编码：0=准备, 1=预冲洗, 2=酶洗, 3=主清洗, 4=漂洗, 5=终末漂洗, 6=干燥, 7=完成
- 状态编码：idle=空闲, running=运行中, paused=已暂停, completed=已完成, error=错误

---

### 5.2 获取任务视频段列表

- **URL**: `GET /task/traceback/{task_id}/segments?video_type=<type>`
- **描述**: 获取任务的所有 HLS 视频段路径和关键点 JSON 路径。

#### 路径参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| task_id | integer | 是 | 任务ID |

#### 查询参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| video_type | string | 否 | "raw" 或 "processed"（默认 "processed"） |

#### 成功响应示例 (200 OK)

```json
{
  "task_id": 123,
  "video_type": "processed",
  "total_segments": 10,
  "playlist_path": "/path/to/playlist.m3u8",
  "segments": [
    {
      "segment_id": 1,
      "segment_path": "/path/to/processed_segment_1706599200.mp4",
      "start_time": 1706599200,
      "end_time": 1706599210,
      "client_id": "192.168.1.100",
      "keypoints_path": "/path/to/keypoints_1706599200.json"
    }
  ]
}
```

**字段说明**:
- `task_id`: 任务ID
- `video_type`: 视频类型（raw=原始视频, processed=处理后视频）
- `total_segments`: 视频段总数
- `playlist_path`: M3U8 播放列表路径
- `segments`: 视频段列表
  - `segment_id`: 段ID（用于获取视频文件）
  - `segment_path`: 视频文件路径
  - `start_time`: 开始时间（Unix 时间戳）
  - `end_time`: 结束时间（Unix 时间戳）
  - `client_id`: 客户端ID
  - `keypoints_path`: 关键点数据文件路径（仅 processed 类型）

#### 错误响应

**404 Not Found** - 未找到视频段
```json
{
  "error": "Resource not found",
  "detail": "未找到任务 123 的视频段",
  "resource_type": "Segment",
  "resource_id": "123"
}
```

**503 Service Unavailable** - 数据库不可用
```json
{
  "error": "Database unavailable",
  "detail": "Database error: Failed to query segments for task 123",
  "retryable": true
}
```

#### cURL 示例

```bash
# 获取处理后的视频段
curl -X GET "http://localhost:8000/task/traceback/123/segments?video_type=processed"

# 获取原始视频段
curl -X GET "http://localhost:8000/task/traceback/123/segments?video_type=raw"
```

#### 注意事项

- `processed` 类型包含 AI 标注和关键点数据
- `raw` 类型是未经处理的原始视频
- 返回的视频段按时间顺序排列

---

### 5.3 获取任务播放列表

- **URL**: `GET /task/traceback/{task_id}/playlist?video_type=<type>`
- **描述**: 获取任务的 HLS 播放列表文件（.m3u8），可直接用于视频播放器。

#### 路径参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| task_id | integer | 是 | 任务ID |

#### 查询参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| video_type | string | 否 | "raw" 或 "processed"（默认 "processed"） |

#### 成功响应

返回 M3U8 播放列表文件（Content-Type: `application/vnd.apple.mpegurl`）

#### 错误响应

**404 Not Found** - 播放列表不存在
```json
{
  "error": "Resource not found",
  "detail": "未找到任务 123 的视频段",
  "resource_type": "Playlist",
  "resource_id": "123"
}
```

#### cURL 示例

```bash
# 下载播放列表
curl -X GET "http://localhost:8000/task/traceback/123/playlist?video_type=processed" \
  -o task_123_processed.m3u8
```

#### HTML5 播放器示例

```html
<video id="player" controls width="640" height="480"></video>

<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
  const video = document.getElementById('player');
  const task_id = 123;
  const playlistUrl = `http://localhost:8000/task/traceback/${task_id}/playlist?video_type=processed`;

  if (Hls.isSupported()) {
    const hls = new Hls();
    hls.loadSource(playlistUrl);
    hls.attachMedia(video);
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Safari 原生支持
    video.src = playlistUrl;
  }
</script>
```

#### 注意事项

- 返回的 M3U8 文件可直接用于 HLS 播放器
- 支持 hls.js、video.js 等主流播放器
- Safari 浏览器原生支持 HLS 播放

---

### 5.4 获取告警记录

- **URL**: `GET /task/{task_id}/alarms`
- **描述**: 查询数据库 `clean_alarm` 表中为指定 `task_id` 保存的所有告警记录，按 `created_at` 降序返回。用于任务回溯与告警审计。

#### 路径参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| task_id | integer | 是 | 任务ID |

#### 成功响应示例 (200 OK)

```json
{
  "task_id": 1,
  "total": 2,
  "alarms": [
    {
      "alarm_id": 1,
      "task_id": 1,
      "step_id": 2,
      "alarm_type": "bubble_detected",
      "message": "检测到气泡，可能存在漏气",
      "severity": "high",
      "resolved": false,
      "resolved_by": null,
      "detected_at": 1706599200,
      "resolved_at": null,
      "created_at": "2025-12-08T20:30:20"
    },
    {
      "alarm_id": 2,
      "task_id": 1,
      "step_id": 1,
      "alarm_type": "bending",
      "message": "检测到内镜弯折",
      "severity": "medium",
      "resolved": true,
      "resolved_by": "admin",
      "detected_at": 1706599100,
      "resolved_at": 1706599150,
      "created_at": "2025-12-08T20:25:00"
    }
  ]
}
```

**字段说明**:
- `alarm_id`: 告警ID
- `task_id`: 任务ID
- `step_id`: 清洗阶段（0-7）
- `alarm_type`: 告警类型（bubble_detected=气泡, bending=弯折等）
- `message`: 告警消息
- `severity`: 严重程度（low=低, medium=中, high=高, critical=严重）
- `resolved`: 是否已解决
- `resolved_by`: 解决人
- `detected_at`: 检测时间（Unix 时间戳）
- `resolved_at`: 解决时间（Unix 时间戳）
- `created_at`: 创建时间（ISO 8601 格式）

#### cURL 示例

```bash
curl -X GET "http://localhost:8000/task/1/alarms"
```

#### 注意事项

- 告警记录按创建时间降序排列（最新的在前）
- 即使查询失败也会返回空列表，不会抛出异常
- 告警类型由推理模型和时序分析逻辑触发

---

## 附录

### A. 清洗阶段编码

| 编码 | 名称 | 说明 |
|------|------|------|
| 0 | 准备阶段 | 任务准备和初始化 |
| 1 | 预冲洗 | 初步冲洗 |
| 2 | 酶洗 | 酶液清洗 |
| 3 | 主清洗 | 主要清洗过程 |
| 4 | 漂洗 | 清水漂洗 |
| 5 | 终末漂洗 | 最后漂洗 |
| 6 | 干燥 | 干燥处理 |
| 7 | 完成 | 清洗完成 |

### B. 任务状态编码

| 编码 | 名称 | 说明 |
|------|------|------|
| idle | 空闲 | 无活跃任务 |
| running | 运行中 | 任务正在执行 |
| paused | 已暂停 | 任务暂停 |
| completed | 已完成 | 任务完成 |
| cancelled | 已取消 | 任务取消 |
| error | 错误 | 任务异常 |
| terminated | 已终止 | 任务终止 |

### C. 完整使用流程示例

#### 方式一：使用统一 API（推荐）

```python
import requests
import websocket
import json

# 1. 启动任务和流（统一接口，一步完成）
task_id = 1
start_config = {
    "task_id": task_id,
    "rtsp_url": "rtsp://localhost:8554/live/stream",
    "fps": 30
}
response = requests.post("http://localhost:8000/api/start", json=start_config)
result = response.json()
client_id = result["client_id"]
print(f"任务启动: {result}")

# 2. 连接 WebSocket 接收推理结果
def on_video_message(ws, message):
    print(f"收到视频帧: {len(message)} bytes")

video_ws = websocket.WebSocketApp(
    f"ws://localhost:8000/ai/video?client_id={client_id}",
    on_message=on_video_message
)

# 3. 连接 WebSocket 监控任务状态
def on_status_message(ws, message):
    status = json.loads(message)
    print(f"任务状态: {status}")

status_ws = websocket.WebSocketApp(
    f"ws://localhost:8000/task/status/{client_id}",
    on_message=on_status_message
)

# 4. 查询系统状态
response = requests.get("http://localhost:8000/health/status")
print(f"系统状态: {response.json()}")

# 5. 任务完成后终止（统一接口，完整清理）
response = requests.post(f"http://localhost:8000/api/terminate?client_id={client_id}")
print(f"任务终止: {response.json()}")

# 6. 查询告警记录
response = requests.get(f"http://localhost:8000/task/{task_id}/alarms")
alarms = response.json()
print(f"告警记录: {alarms['total']} 条")
```

#### 方式二：使用传统 API（过渡期保留）

```python
import requests
import websocket
import json

# 1. 加载任务（⚠️ 将弃用）
task_id = 1
response = requests.get(f"http://localhost:8000/ai/load_task/{task_id}")
print(f"任务加载: {response.json()}")

# 2. 启动 RTSP 流（⚠️ 将弃用）
client_id = "192.168.1.100"
rtsp_config = {
    "client_id": client_id,
    "rtsp_url": "rtsp://localhost:8554/live/stream",
    "fps": 30
}
response = requests.post("http://localhost:8000/inspection/start_rtsp_stream", json=rtsp_config)
print(f"流启动: {response.json()}")

# 3. 连接 WebSocket 接收推理结果
def on_video_message(ws, message):
    print(f"收到视频帧: {len(message)} bytes")

video_ws = websocket.WebSocketApp(
    f"ws://localhost:8000/ai/video?client_id={client_id}",
    on_message=on_video_message
)

# 4. 连接 WebSocket 监控任务状态
def on_status_message(ws, message):
    status = json.loads(message)
    print(f"任务状态: {status}")

status_ws = websocket.WebSocketApp(
    f"ws://localhost:8000/task/status/{client_id}",
    on_message=on_status_message
)

# 5. 任务完成后停止流（⚠️ 将弃用）
response = requests.post(f"http://localhost:8000/inspection/stop_rtsp_stream?client_id={client_id}")
print(f"流停止: {response.json()}")

# 6. 终止任务并清理资源（⚠️ 将弃用，且不完整）
response = requests.post(f"http://localhost:8000/ai/terminate_task/{client_id}")
print(f"任务终止: {response.json()}")

# 7. 查询告警记录
response = requests.get(f"http://localhost:8000/task/{task_id}/alarms")
alarms = response.json()
print(f"告警记录: {alarms['total']} 条")
```

---

**最后更新**: 2026-02-08
