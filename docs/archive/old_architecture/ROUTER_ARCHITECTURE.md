# 路由架构设计

## 概述

本项目按照**服务模块**划分路由，每个路由文件负责对应服务的生命周期管理和 API 接口。所有 HTTP/WS 请求在进入路由层前会先经过 `GatewayMiddleware`（[app/utils/gateway.py](../app/utils/gateway.py)）做 IP 白名单、速率限制与反扫描检查，详见 [API_GATEWAY.md](API_GATEWAY.md)。

## 路由划分原则

| 路由文件 | 服务模块 | 职责 | Lifespan 管理 |
| --- | --- | --- | --- |
| **health.py** | GlobalHealthMonitor | 流健康监控 + 孤儿流检测 + 自动重连 | ✅ 是 |
| **ai.py** | InferenceManager | WebSocket 推理结果推送 `/ai/video` | ✅ 是 |
| **task.py** | Database | 任务 CRUD 操作 | ❌ 否 |
| **api.py** | 多模块协调 | 统一启动/终止 API（协调 Stream + Inference + ClientManager） | ❌ 否 |

> **2026-04 变更**：原 `inspection.py` 路由已整体下线，其流启停功能合并到 `/api/start` 与 `/api/terminate`；`ai.py` 中的 `/ai/load_task` `/ai/terminate_task` `/ai/status` 等过渡接口同步移除，`/ai/video` WebSocket 保留。`/docs` `/redoc` `/openapi.json` 已永久关闭。

## 生命周期管理顺序

在 `main.py` 中，服务按照**依赖顺序**启动：

```python
async with health.lifespan():  # 1. 健康监控（依赖 client_manager, stream_service, inference_manager）
    async with ai.lifespan():  # 2. AI 推理服务
        yield
```

**为什么健康监控先启动？**
- GlobalHealthMonitor 依赖其他三个服务（client_manager, stream_service, inference_manager）
- 但这三个服务都是全局单例，在应用启动时就已创建（非懒加载）
- 健康监控需要在 AI 推理服务之前启动，确保流健康状态能被及时监控

## 路由详细说明

### 1. health.py - 健康监控路由

**职责：**
- 管理 GlobalHealthMonitor 的生命周期
- 提供健康监控统计信息查询
- 提供配置信息查询

**API 接口：**

```bash
# 查询监控统计信息
GET /health/monitor/stats

# 返回示例
{
  "status": "running",
  "checks": 12345,
  "suspects": 5,
  "cleanups": 2,
  "reconnects": 10,
  "reconnect_successes": 8,
  "orphans_detected": 1,
  "reconnecting_count": 0,
  "reconnecting_clients": []
}

# 查询监控配置
GET /health/monitor/config

# 返回示例
{
  "status": "running",
  "config": {
    "check_interval": 1.0,
    "heartbeat_timeout": 5.0,
    "restart_delay": 5.0,
    "max_restart_attempts": 5,
    "orphan_timeout": 30.0
  },
  "derived": {
    "suspect_timeout": 5.0,
    "cleanup_timeout": 30.0,
    "reconnect_interval": 5.0,
    "max_reconnect_attempts": 5
  }
}
```

**生命周期管理：**

```python
@asynccontextmanager
async def lifespan():
    """全局健康监控服务生命周期管理"""
    global _health_monitor

    # 创建并启动健康监控
    _health_monitor = GlobalHealthMonitor(
        client_manager=client_manager,
        stream_service=stream_service,
        inference_manager=ai.manager
    )
    _health_monitor.start()

    try:
        yield
    finally:
        if _health_monitor:
            _health_monitor.stop()
```

### 2. ai.py - AI 推理路由

**职责：**
- 管理 InferenceManager 的生命周期
- WebSocket 推理结果推送

**主要接口：**

```bash
# WebSocket 推理结果推送（唯一对外接口）
WS /ai/video?client_id=xxx
```

> WebSocket handler 内置 `_recv_until_disconnect()` 后台任务，监听客户端 CLOSE 帧以配合 lifespan 优雅关闭，避免 Ctrl-C 时与 shutdown_event 形成环等待。详见 [EXCEPTION_FLOW_StreamService.md](EXCEPTION_FLOW_StreamService.md)。

### 3. task.py - 任务管理路由

**职责：**
- 任务 CRUD 操作
- 与数据库交互

**主要接口：**

```bash
# 创建任务
POST /task

# 查询任务列表
GET /task

# 查询单个任务
GET /task/{task_id}

# 更新任务
PUT /task/{task_id}

# 删除任务
DELETE /task/{task_id}
```

### 4. api.py - 统一 API 路由（主入口）

**职责：**
- 提供统一的启动和终止接口
- 协调多个模块（Stream + Inference + ClientManager）
- 原子化操作，避免部分失败

**主要接口：**

```bash
# 统一启动接口（一步完成：加载任务 + 启动流）
POST /api/start
Body: {
  "task_id": 1,
  "rtsp_url": "rtsp://...",
  "fps": 30
}

# 统一终止接口（完整清理：解码器 + 推理 + ClientManager）
POST /api/terminate?client_id=xxx
```

## 架构优势

### 1. 职责清晰

每个路由文件对应一个服务模块，职责单一：

- ✅ `health.py` → `GlobalHealthMonitor`
- ✅ `ai.py` → `InferenceManager`（WebSocket 结果推送）
- ✅ `task.py` → `Database`
- ✅ `api.py` → 多模块协调（Stream + Inference + ClientManager）

### 2. 生命周期独立管理

每个有后台线程的服务都在对应路由的 lifespan 中管理：

- ✅ `GlobalHealthMonitor` 在 `health.py` 中启动和停止
- ✅ `InferenceManager` 在 `ai.py` 中启动和停止
- ✅ 启动顺序清晰：health → ai

### 3. 无全局变量污染

不使用全局变量和延迟初始化模式：

```python
# ❌ 旧方式（全局变量）
global_health_monitor: Optional[GlobalHealthMonitor] = None

def init_global_health_monitor(...):
    global global_health_monitor
    if global_health_monitor is None:
        global_health_monitor = GlobalHealthMonitor(...)

# ✅ 新方式（路由内部变量）
_health_monitor: GlobalHealthMonitor | None = None

@asynccontextmanager
async def lifespan():
    global _health_monitor
    _health_monitor = GlobalHealthMonitor(...)
    _health_monitor.start()
    try:
        yield
    finally:
        _health_monitor.stop()
```

### 4. API 接口组织清晰

用户可以根据功能类别快速找到对应接口：

- `/api/*` - 统一 API（主入口，`/api/start` 与 `/api/terminate`）
- `/ai/video` - AI 推理 WebSocket
- `/task/*` - 任务管理 / 溯源查询
- `/health/*` - 健康监控

## 运维与调用

### 监控健康状态

```python
# 新增：查询监控统计
response = requests.get("http://localhost:8000/health/monitor/stats")
stats = response.json()

print(f"检查次数: {stats['checks']}")
print(f"重连次数: {stats['reconnects']}")
print(f"孤儿流检测: {stats['orphans_detected']}")
```

## 文件结构

```
app/
├── routers/
│   ├── health.py      # 健康监控路由
│   ├── ai.py          # AI 推理 WebSocket 路由
│   ├── task.py        # 任务管理路由
│   └── api.py         # 统一 API 路由（主入口）
├── services/
│   ├── health_monitor/        # GlobalHealthMonitor
│   ├── inference/             # InferenceManager
│   ├── stream/                # StreamService
│   └── client/                # ClientManager
├── utils/
│   └── gateway.py     # GatewayMiddleware（ASGI 安全层）
└── main.py            # 应用入口（组合所有 lifespan + 挂载 Gateway）
```

## 相关文档

- [API_GATEWAY.md](API_GATEWAY.md) - API Gateway 安全层
- [ARCHITECTURE_API_SURFACE.md](kb/ARCHITECTURE_API_SURFACE.md) - 当前全部接口清单
- [API_MIGRATION_GUIDE.md](archive/completed_refactoring/API_MIGRATION_GUIDE.md) - API 迁移指南（历史）
- [SERVICE_HEALTH_MONITOR.md](kb/SERVICE_HEALTH_MONITOR.md) - 健康监控重构说明
