# HealthMonitor 重构：升级为全局服务

## 问题背景

### 当前问题
重连超时后，HealthMonitor 只停止解码器，但保留 ClientManager：
```
[HealthMonitor] Decoder stopped. Call terminate API to clean client data.
[ModelWorkerService] 客户端列表已刷新: 1 个客户端  ← 客户端还在！
```

**导致的问题**：
1. **资源浪费**：ClientManager 保留旧帧，占用内存
2. **GPU 浪费**：推理服务重复推理最后一帧（无去重机制）
3. **不合理**：5次重连失败，流基本不可恢复，保留资源没有意义

### 架构问题

**旧架构**（职责越界）：
```
StreamService
  └─ HealthMonitor (清理 Stream + Inference + ClientManager) ❌
```

- HealthMonitor 隶属于 StreamService
- 但需要清理 InferenceManager 和 ClientManager
- 违反了模块边界

## 解决方案

### 新架构：HealthMonitor 升级为全局服务

```
全局服务层
  ├─ StreamService (管理流和解码器)
  ├─ InferenceManager (管理推理)
  ├─ ClientManager (管理队列)
  └─ GlobalHealthMonitor (监控健康 + 协调清理) ✅
```

**职责**：
- **GlobalHealthMonitor**：独立的全局服务
  - 监控所有客户端的流健康
  - 检测断流并自动重连
  - 重连失败后**完整清理**（Stream + Inference + ClientManager）
  - 类似于 API 层，有权限协调多个模块

### 重连失败处理逻辑

**旧逻辑**（不完整）：
```python
def _exit_reconnect_mode(cleanup=True):
    # 只停止解码器
    stream_service.stop_stream(client_id)
    # ClientManager 保留 ❌
```

**新逻辑**（完整清理）：
```python
def _cleanup_failed_client(client_id):
    """重连失败后完整清理（类似 /api/terminate）"""

    # 1. 停止解码器
    stream_service.stop_stream(client_id)

    # 2. 落盘残余数据
    inference_manager.remove_client(client_id)

    # 3. 清理 ClientManager
    client_manager.remove_client(client_id, cleanup=True)

    logger.error(
        f"STREAM CONNECTION FAILED: {client_id}\n"
        f"Reason: Reconnect failed after 5 attempts\n"
        f"Action: Full cleanup completed. Call /api/start to restart."
    )
```

## 实现步骤

### 1. 创建全局 HealthMonitor

**新文件**：`app/services/health_monitor.py`

```python
class GlobalHealthMonitor:
    """全局健康监控服务"""

    def __init__(self, client_manager, stream_service, inference_manager, config):
        self._client_manager = client_manager
        self._stream_service = stream_service
        self._inference_manager = inference_manager
        # ...

    def _cleanup_failed_client(self, client_id):
        """完整清理（类似 /api/terminate）"""
        # 1. 停止解码器
        # 2. 落盘数据
        # 3. 清理 ClientManager
```

### 2. 在应用启动时初始化

**修改**：`app/routers/ai.py`

```python
@asynccontextmanager
async def lifespan():
    # 启动 AI 推理服务
    ai.start()

    # 初始化并启动全局健康监控
    from app.services.health_monitor import init_global_health_monitor
    init_global_health_monitor(
        client_manager=client_manager,
        stream_service=stream_service,
        inference_manager=ai.manager
    )
    global_health_monitor.start()

    yield

    # 停止
    global_health_monitor.stop()
    ai.stop()
```

### 3. 移除 StreamService 对旧 HealthMonitor 的依赖

**修改**：`app/services/stream/service.py`

- 移除 `self.health_monitor` 初始化
- 移除 `ensure_health_monitor_started()` 方法
- `shutdown()` 中移除 `health_monitor.stop()` 调用

## 优势

### 1. 职责清晰
- **StreamService**：只管理流资源
- **GlobalHealthMonitor**：监控 + 协调清理

### 2. 资源高效
- 重连失败后完整清理
- 不再浪费内存和 GPU

### 3. 架构合理
- GlobalHealthMonitor 作为全局服务，有权限协调多个模块
- 类似于 API 层（`/api/terminate`），但自动化执行

## 测试验证

### 场景：流断开 + 重连失败

1. 启动流：`POST /api/start`
2. 模拟断流：关闭 RTSP 源
3. 等待重连失败（5次，25秒）

**预期日志**：
```
[GlobalHealthMonitor] SUSPECT: client_id, no frames for 5.0s, entering reconnect mode
[GlobalHealthMonitor] RECONNECT: client_id, attempt 1/5
[GlobalHealthMonitor] RECONNECT: client_id, attempt 2/5
...
[GlobalHealthMonitor] TIMEOUT: client_id, giving up reconnect
[GlobalHealthMonitor] ⚠️  STREAM CONNECTION FAILED: client_id
  Reason: Reconnect failed after 5 attempts
  Action: Executing full cleanup...
[GlobalHealthMonitor] Decoder stopped: client_id
[GlobalHealthMonitor] Data flushed: client_id
[GlobalHealthMonitor] ClientManager cleaned: client_id
[GlobalHealthMonitor] Full cleanup completed: client_id
  Action: Call /api/start to restart the stream.
```

**预期状态**：
- ✅ 解码器已停止
- ✅ 残余数据已落盘
- ✅ ClientManager 已清理
- ✅ 推理服务不再看到该客户端
- ✅ 内存已释放

## 相关文件

### 新增
- `app/services/health_monitor.py` - 全局健康监控服务

### 修改
- `app/routers/ai.py` - 应用启动时初始化全局健康监控
- `app/services/stream/service.py` - 移除旧 HealthMonitor 依赖

### 废弃（可选保留）
- `app/services/stream/health_monitor.py` - 旧的 HealthMonitor（StreamService 子模块）

## 迁移建议

### 阶段 1：创建全局 HealthMonitor ✅
- 创建 `app/services/health_monitor.py`
- 实现完整清理逻辑

### 阶段 2：更新应用启动
- 修改 `ai.py` lifespan
- 在全局层面初始化和启动

### 阶段 3：清理旧代码（可选）
- 移除 `StreamService.ensure_health_monitor_started()`
- 移除 `StreamService.health_monitor`
- 考虑废弃 `app/services/stream/health_monitor.py`

## 后续优化（可选）

### 1. 推理服务去重
为 InferenceManager 添加停滞检测：
```python
if cq.latest_raw_timestamp == self._last_timestamp[client_id]:
    logger.debug(f"Skip duplicate frame: {client_id}")
    continue
```

### 2. 可配置的清理策略
```python
class HealthMonitorConfig:
    cleanup_on_reconnect_failure: bool = True  # 是否完整清理
    auto_restart_on_recovery: bool = False  # 是否自动重启
```
