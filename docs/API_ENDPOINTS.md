<!-- markdownlint-configure-file {"MD060": false} -->

# CleanSight API 端点文档

本文档详细说明了 CleanSight 后端提供的所有 API 接口，包括请求格式、参数说明、响应示例和使用方法。

## 概述

CleanSight 后端提供 RESTful API 和 WebSocket 接口，用于管理内镜清洗任务、实时视频流推理和历史数据回溯。

### 基础信息

- **基础URL**: `http://localhost:8000` (开发环境)
- **协议**: HTTP/1.1 和 WebSocket
- **API 文档端点**: `/docs`、`/redoc`、`/openapi.json` 已**永久关闭**，接口清单以本文档为准
- **安全网关**: 所有 HTTP/WS 请求先经 `GatewayMiddleware`（IP 白名单 / 速率限制 / 反扫描），详见 [API_GATEWAY.md](API_GATEWAY.md)

### 接口分区（2026-05 起）

- `/api/*` —— 统一启动/终止（主入口）
- `/ai/video` —— AI 推理 WebSocket
- `/task/*` —— 任务状态 / 告警
- `/health/*` —— 健康监控（Gateway 宽松路径，可高频轮询）
- `/traceback/*` —— 视频追溯（告警证据 / VOD 回放 / 时间轴打点）
- `/media/*` —— 媒体访问层（token 化 HLS 段 / keypoints JSON，由追溯接口签发）

**已下线的接口**（本文档不再描述）：`/inspection/*`、`/ai/status`、`/ai/load_task`、`/ai/terminate_task`。功能已合并进 `/api/start` 与 `/api/terminate`。

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

## 三、AI 推理服务 (`/ai`)

AI 推理服务对外仅保留 WebSocket `/ai/video`，用于向前端实时推送处理后的视频帧。任务加载、终止、状态查询等功能已合并到 `/api/*` 与 `/health/*`。

### 3.1 实时推理结果流（WebSocket）

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

- 必须先通过 `POST /api/start` 启动任务与流
- WebSocket 会自动处理帧率控制和去重
- 服务端内置 `_recv_until_disconnect` 监听 CLOSE 帧，客户端断开后自动清理
- 服务端 lifespan 关闭时通过 `shutdown_event` 与 CLOSE 帧双重保证及时退出（见 [EXCEPTION_FLOW_StreamService.md](EXCEPTION_FLOW_StreamService.md)）

---

## 四、任务管理 (`/task`)

任务管理服务提供任务状态监控和历史视频回溯功能。

### 4.1 任务状态实时更新（WebSocket）

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

### 4.2 获取告警记录

- **URL**: `GET /task/{task_id}/alarms`
- **描述**: 始终查询数据库 `clean_alarm` 表（告警由 AlarmWorker 实时异步写入，秒级延迟），按 `created_at` 降序返回。用于任务回溯与告警审计。

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

### 4.3 前端实时消息（HTTP 轮询）

- **URL**: `GET /task/message/{task_id}?since_seq=<seq>`
- **用途**: 前端实时告警提示，按 seq 增量拉取（建议 1~2 Hz）。返回内存快照：检测结果、时序事件、最近 5 条内存告警。

> `/task/message/*` 属于 Gateway **宽松路径**（默认 `gateway_relaxed_prefixes="/health,/task/message,/admin-f3m8,/metrics,/media"`），高频轮询不会触发升级封禁。详见 [API_GATEWAY.md](API_GATEWAY.md)。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| task_id | integer | — | 任务 ID（路径参数）|
| since_seq | integer | 0 | 上次返回的 max_seq，用于增量更新 |

**响应结构**：

```json
{
  "task_id": 1,
  "max_seq": 5,
  "signals_10s": { "bubble": { "active": true, "hit_count": 3, "max_conf": 0.92 } },
  "alarms": [
    { "seq": 5, "alarm_type": "bubble_detected", "severity": "high", "message": "..." }
  ]
}
```

**任务不活跃时**（task_id 无对应活跃客户端）返回 `max_seq: 0`、空 `alarms`。

---

## 五、视频追溯（`/traceback`）

视频追溯服务提供基于文件系统的告警证据回溯、任务 VOD 回放与时间轴打点。
详细设计见 [TRACEBACK_API.md](TRACEBACK_API.md)。

### 5.1 告警证据回溯

- **URL**: `GET /traceback/alarm/{alarm_id}/evidence`
- **描述**: 拉一条告警的原始 + 处理后双轨视频片段与推理 keypoints，用于误报验证。

#### 路径参数

| 参数 | 类型 | 说明 |
|------|------|------|
| alarm_id | integer | 告警 ID |

#### 查询参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| n_before | integer | 1 | 触发段前上下文段数 |
| n_after | integer | 2 | 触发段后上下文段数 |

#### 成功响应示例 (200 OK)

```json
{
  "alarm": { "alarm_id": 42, "task_id": 100, "severity": "high", "detected_at": 1700000022000 },
  "client_id": "192.168.1.10",
  "raw_clips":       [{ "url": "http://host/media/segment/<token>", "ts_us": 1700000020000000, "is_trigger": true }],
  "processed_clips": [{ "url": "http://host/media/segment/<token>", "ts_us": 1700000020000000, "is_trigger": true }],
  "keypoints_url": "http://host/media/keypoints/<token>",
  "detection": [{ "timestamp": 1700000020.0, "keypoints": {}, "inference_result": {} }]
}
```

#### 错误响应

| 状态码 | 场景 |
|--------|------|
| 404 | 告警不存在 / 任务无 source_ip |
| 503 | 数据库不可用 |

---

### 5.2 任务完整回放 Playlist

- **URL**: `GET /traceback/task/{task_id}/playlist.m3u8?track=processed`
- **描述**: 动态生成任务全程 VOD m3u8，带 `#EXT-X-ENDLIST`，前端 hls.js 直接消费。

#### 路径参数

| 参数 | 类型 | 说明 |
|------|------|------|
| task_id | integer | 任务 ID |

#### 查询参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| track | string | processed | `raw` 或 `processed` |

#### 成功响应 (200 OK)

`Content-Type: application/vnd.apple.mpegurl`，VOD m3u8 文件。

```javascript
// hls.js 接入示例
const hls = new Hls();
hls.loadSource(`/traceback/task/${taskId}/playlist.m3u8?track=processed`);
hls.attachMedia(document.getElementById('player'));
```

#### 错误响应

| 状态码 | 场景 |
|--------|------|
| 404 | 任务不存在 / 无可用段 |
| 422 | track 参数非法 |

---

### 5.3 任务时间轴打点

- **URL**: `GET /traceback/task/{task_id}/timeline`
- **描述**: 返回任务时间范围与告警事件列表，供前端在进度条上叠加标记。

#### 成功响应示例 (200 OK)

```json
{
  "task_id": 100,
  "client_id": "192.168.1.10",
  "start_ms": 1700000000000,
  "end_ms": 1700002400000,
  "duration_ms": 2400000,
  "events": [
    {
      "ts_ms": 1700000022000,
      "type": "alarm",
      "alarm_id": 42,
      "alarm_type": "bubble_detected",
      "severity": "high"
    }
  ]
}
```

#### 错误响应

| 状态码 | 场景 |
|--------|------|
| 404 | 任务不存在 |

---

## 六、媒体访问层（`/media`）

媒体访问层是前后端物理隔离的关键：所有 MP4 段和 keypoints JSON 均通过 token 化 URL 返回，
不暴露文件系统路径。Token 由 `/traceback/*` 接口签发，前端只需跟随返回的 URL 即可。

### 6.1 流式返回 MP4 段

- **URL**: `GET /media/segment/{token}`
- **描述**: 返回单个 MP4 视频段。Token 由 `/traceback/alarm/{id}/evidence` 或 `/traceback/task/{id}/playlist.m3u8` 签发。

#### 成功响应 (200 OK)

`Content-Type: video/mp4`，MP4 文件流（hls.js 自动拉取，无需手动调用）。

#### 错误响应

| 状态码 | 场景 |
|--------|------|
| 403 | token 无效 / 签名不符 / 已过期 |
| 404 | 文件不存在（已清理） |

---

### 6.2 返回 Keypoints JSON

- **URL**: `GET /media/keypoints/{token}`
- **描述**: 返回单个 keypoints JSON 文件。Token 由 `/traceback/alarm/{id}/evidence` 签发（`keypoints_url` 字段）。

#### 成功响应 (200 OK)

```json
[
  { "timestamp": 1700000020.5, "keypoints": {}, "inference_result": {} }
]
```

#### 错误响应

| 状态码 | 场景 |
|--------|------|
| 403 | token 无效 / 已过期 |
| 404 | 文件不存在 |

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

#### 统一 API 完整流程

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

---

**最后更新**: 2026-05-04
