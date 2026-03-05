# API 迁移指南

## 概述

本次架构重构实现了：
1. **职责分离**：各模块职责单一，不越界调用
2. **API 简化**：提供统一的启动和终止接口

## 新旧 API 对比

### 启动流程

#### 旧方式（两步）
```bash
# 步骤 1: 加载任务
GET /ai/load_task/{task_id}

# 步骤 2: 启动流
POST /inspection/start_rtsp_stream
Body: {"client_id": "xxx", "rtsp_url": "rtsp://...", "fps": 30}
```

**问题**：
- 需要两步操作，容易遗漏
- 跨任务切换时不会自动清理旧数据
- 用户需要手动管理 client_id

#### 新方式（一步）✅
```bash
# 一步完成：加载任务 + 启动流
POST /api/start
Body: {"task_id": 1, "rtsp_url": "rtsp://...", "fps": 30}
```

**优势**：
- ✅ 一步完成所有操作
- ✅ 自动从数据库获取 client_id (source_ip)
- ✅ 自动检测跨任务切换并清理旧数据
- ✅ 原子操作，避免部分失败

---

### 终止流程

#### 旧方式（不完整清理）
```bash
# 只停止流（不清理推理资源和 ClientManager）
POST /inspection/stop_rtsp_stream?client_id=xxx

# 或者只清理推理资源（不停止流）
POST /ai/terminate_task/{client_id}
```

**问题**：
- ❌ `stop_rtsp_stream` 只停止解码器，不落盘数据，不清理 ClientManager
- ❌ `terminate_task` 只落盘数据，不停止解码器，不清理 ClientManager
- ❌ 容易造成资源泄漏（FFmpeg 进程、队列数据）

#### 新方式（完整清理）✅
```bash
# 完整清理：解码器 + 推理 + ClientManager
POST /api/terminate?client_id=xxx
```

**优势**：
- ✅ 停止流解码器（FFmpeg 进程）
- ✅ 落盘残余数据（通过 InferenceManager）
- ✅ 清理 ClientManager（队列数据）
- ✅ "尽力而为"策略，永不抛异常
- ✅ 返回详细的清理结果

---

## 职责划分

### 修改前（职责混乱）

| 模块 | 职责 | 问题 |
|------|------|------|
| InferenceManager | 推理 + 落盘 + **清理 ClientManager** | ❌ 越权清理 |
| StreamService | 流管理 + **清理 ClientManager** | ❌ 越权清理 |
| HealthMonitor | 监控 + **清理 ClientManager** | ❌ 越权清理 |
| CleanupService | 清理 Decoder + **清理 Inference + ClientManager** | ❌ 职责过重 |

### 修改后（职责单一）

| 模块 | 职责 | 清理内容 |
|------|------|---------|
| **StreamService** | 流和解码器管理 | 只停止 FFmpeg 进程 |
| **InferenceManager** | 推理管道管理 | 只落盘数据 + 刷新推理服务 |
| **ClientManager** | 队列资源管理 | 只清空队列数据 |
| **CleanupService** | 流资源清理协调 | 只调用 StreamService |
| **HealthMonitor** | 流健康监控 | 只停止解码器 + 上报失败 |
| **API 层 (/api/terminate)** | 全局清理协调 | 协调三层清理顺序 |

---

## 迁移步骤

### Python 测试脚本

#### 旧代码
```python
from integration_tests.utils import APIClient

api = APIClient()

# 启动
api.start_task(task_id=1)
api.start_rtsp_capture(client_id, rtsp_url, fps=30)

# ... 运行测试 ...

# 清理（不完整）
api.stop_rtsp_capture(client_id)  # 只停止解码器
```

#### 新代码（推荐）✅
```python
from integration_tests.utils import APIClient

api = APIClient()

# 启动（一步完成）
result = api.unified_start(task_id=1, rtsp_url="rtsp://...", fps=30)
client_id = result["client_id"]

# ... 运行测试 ...

# 清理（完整）
api.unified_terminate(client_id)
```

---

### cURL 示例

#### 启动任务
```bash
curl -X POST "http://localhost:8000/api/start" \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": 1,
    "rtsp_url": "rtsp://localhost:8554/live/test",
    "fps": 30
  }'
```

响应：
```json
{
  "status": "success",
  "client_id": "192.168.1.100",
  "task_id": 1,
  "rtsp_url": "rtsp://localhost:8554/live/test",
  "message": "Task 1 started for client 192.168.1.100"
}
```

#### 终止任务
```bash
curl -X POST "http://localhost:8000/api/terminate?client_id=192.168.1.100"
```

响应：
```json
{
  "status": "success",
  "client_id": "192.168.1.100",
  "decoder_stopped": true,
  "data_flushed": true,
  "client_cleaned": true,
  "errors": []
}
```

---

## 旧 API 兼容性

旧接口已标记为**过渡接口**，暂时保留以确保向后兼容，但已修正清理逻辑：

### GET /ai/load_task/{task_id}
- ⚠️ 过渡接口
- 说明：只加载任务，不启动流
- 建议：使用 `POST /api/start` 代替

### POST /inspection/start_rtsp_stream
- ⚠️ 过渡接口
- 说明：只启动流，不加载任务
- 建议：使用 `POST /api/start` 代替

### POST /inspection/stop_rtsp_stream
- ⚠️ 过渡接口
- **已修正**：现在会执行完整清理（解码器 + 推理 + ClientManager）
- 建议：使用 `POST /api/terminate` 代替

### POST /ai/terminate_task/{client_id}
- ⚠️ 过渡接口
- 说明：只清理推理资源，不停止解码器
- 建议：使用 `POST /api/terminate` 代替

---

## 测试脚本示例

新增测试脚本展示统一 API 的使用：

```bash
# 运行新 API 测试
python integration_tests/example_unified_api.py --task_id 1 --duration 30
```

---

## 常见问题

### Q: 旧接口什么时候移除？
A: 旧接口会保留一段时间以确保向后兼容。建议在下个版本之前完成迁移。

### Q: 如果我只想停止流，不清理其他资源？
A: 可以直接调用 `StreamService.stop_stream(client_id)`，但通常不推荐这样做。建议使用 `POST /api/terminate` 进行完整清理。

### Q: 跨任务切换时旧数据会怎么处理？
A: 新 API (`POST /api/start`) 会自动检测跨任务切换，并清理旧队列数据。

### Q: 如果清理失败会怎样？
A: 新 API 采用"尽力而为"策略，即使某步骤失败也会继续执行后续步骤，并在响应中返回详细的错误信息。

---

## 相关文件

### 核心实现
- `app/routers/api.py` - 新统一 API 路由
- `app/services/inference/core/manager.py` - InferenceManager（移除 ClientManager 清理）
- `app/services/stream/service.py` - StreamService（移除 ClientManager 清理）
- `app/services/stream/cleanup.py` - CleanupService（简化职责）
- `app/services/stream/health_monitor.py` - HealthMonitor（只清理解码器）

### 测试工具
- `integration_tests/utils.py` - APIClient（新增 unified_start 和 unified_terminate）
- `integration_tests/example_unified_api.py` - 新 API 使用示例

---

## 升级建议

1. **立即行动**：更新集成测试脚本使用新 API
2. **验证清理**：确认 terminate 后 FFmpeg 进程、队列数据都被清理
3. **监控日志**：观察 `[start]` 和 `[terminate]` 日志，确认清理流程正确
4. **逐步迁移**：可以先在测试环境验证，再推广到生产环境
