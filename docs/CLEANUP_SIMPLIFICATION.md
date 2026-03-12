# 清理服务简化重构

## 概述

本次重构将原本分散的清理和健康监控逻辑全部整合到 `GlobalHealthMonitor` 中，移除了中间层。

## 重构内容

### 删除的模块

1. **app/services/stream/cleanup.py** - CleanupService
   - 原职责：解码器清理 + 孤儿流检测
   - 问题：作为中间层没有增加实际价值

2. **app/services/stream/health_monitor.py** - StreamHealthMonitor（旧版）
   - 原职责：流健康监控（StreamService 子模块）
   - 问题：作为 StreamService 子模块，无法协调其他模块清理

### 新的统一架构

所有健康监控和清理职责现在集中在 **GlobalHealthMonitor** (`app/services/health_monitor.py`)：

```
GlobalHealthMonitor（全局单例）
├─ 流健康监控
│  ├─ 检测断流（超时未收到帧）
│  └─ 自动重连（最多 5 次）
├─ 孤儿流检测
│  ├─ 检测有 ClientQueues 但无 Decoder 的客户端
│  └─ 超时后自动清理
└─ 完整清理协调
   ├─ 停止解码器（StreamService）
   ├─ 落盘数据（InferenceManager）
   └─ 清理队列（ClientManager）
```

## 职责清晰度对比

### 修改前

| 模块 | 职责 | 问题 |
|------|------|------|
| StreamHealthMonitor | 流健康监控 | ❌ 属于 StreamService，无法清理其他模块 |
| CleanupService | 解码器清理 + 孤儿流检测 | ❌ 中间层，增加复杂度 |
| GlobalHealthMonitor | 流健康监控 + 重连 | ❌ 重连失败时无法完整清理 |

### 修改后

| 模块 | 职责 |
|------|------|
| **GlobalHealthMonitor** | 流健康监控 + 自动重连 + 孤儿流检测 + 完整清理协调 |
| StreamService | 解码器管理（纯粹的流服务） |
| InferenceManager | 推理管道管理 |
| ClientManager | 队列资源管理 |

## 关键变化

### 1. GlobalHealthMonitor 增强

**新增功能：孤儿流检测**

```python
class GlobalHealthMonitor:
    def _check_all_clients(self):
        """检查所有客户端的健康状态（含孤儿流检测）"""
        active_decoders = set(self._stream_service.get_all_client_ids())

        for client_id, cq in all_clients.items():
            has_decoder = client_id in active_decoders

            if has_decoder:
                # 有解码器：检查流健康
                self._handle_stream_health(client_id, cq, current_time)
            else:
                # 无解码器：检查是否为孤儿流
                self._handle_potential_orphan(client_id, cq, current_time)
```

**孤儿流处理**：

```python
def _handle_potential_orphan(self, client_id: str, cq, current_time: float):
    """处理孤儿流（有 ClientQueues 但没有 Decoder）"""
    idle_time = current_time - cq.latest_raw_timestamp

    if idle_time >= self.orphan_timeout:
        # 孤儿流没有解码器，只需清理推理和 ClientManager
        self._inference_manager.remove_client(client_id)
        self._client_manager.remove_client(client_id, cleanup=True)
```

### 2. StreamService 简化

**删除的代码**：

```python
# ❌ 删除
self.cleanup_service = None
self._ensure_cleanup_service()
```

**结果**：StreamService 现在是纯粹的流管理服务，不再管理任何清理或健康监控逻辑。

### 3. API 层清理逻辑内联

**app/routers/inspection.py** 中的 `stop_stream` 接口现在直接调用三层清理：

```python
@router.post("/stop_stream")
async def stop_stream(client_id: str):
    """⚠️ 过渡接口：建议使用 POST /api/terminate 代替"""

    # 1. 停止解码器
    stream_service.stop_stream(client_id)

    # 2. 落盘残余数据
    ai.remove_client(client_id)

    # 3. 清理 ClientManager
    client_manager.remove_client(client_id, cleanup=True)
```

## 配置更新

**HealthMonitorConfig** 新增孤儿流超时配置：

```python
class HealthMonitorConfig:
    def __init__(
        self,
        check_interval: float = 1.0,
        heartbeat_timeout: float = 5.0,
        restart_delay: float = 5.0,
        max_restart_attempts: int = 5,
        orphan_timeout: float = 30.0  # ← 新增
    ):
        ...
```

## 统计信息

GlobalHealthMonitor 统计信息新增 `orphans_detected` 字段：

```python
self._stats = {
    "checks": 0,
    "suspects": 0,
    "cleanups": 0,
    "reconnects": 0,
    "reconnect_successes": 0,
    "orphans_detected": 0  # ← 新增
}
```

## 初始化流程

**app/routers/ai.py** 中的 lifespan 管理：

```python
@asynccontextmanager
async def lifespan():
    ai.start()

    # 初始化全局健康监控（包含所有清理逻辑）
    from app.services.health_monitor import init_global_health_monitor, global_health_monitor
    init_global_health_monitor(client_manager, stream_service, ai.manager)
    if global_health_monitor:
        global_health_monitor.start()

    yield

    if global_health_monitor:
        global_health_monitor.stop()
    ai.stop()
```

## 日志示例

### 孤儿流检测

```
[GlobalHealthMonitor] ORPHAN STREAM detected: 172.16.77.221, idle for 35.2s (no decoder), cleaning up
[GlobalHealthMonitor] Orphan data flushed: 172.16.77.221
[GlobalHealthMonitor] Orphan ClientManager cleaned: 172.16.77.221
[GlobalHealthMonitor] Orphan cleanup completed: 172.16.77.221
```

### 流健康监控

```
[GlobalHealthMonitor] SUSPECT: 192.168.1.100, no frames for 5.3s, entering reconnect mode
[GlobalHealthMonitor] RECONNECT: 192.168.1.100, attempt 1/5
[GlobalHealthMonitor] SUCCESS: 192.168.1.100 reconnected (attempt 1/5)
```

### 重连失败

```
[GlobalHealthMonitor] TIMEOUT: 192.168.1.100, no frames for 30.5s, giving up reconnect
[GlobalHealthMonitor] ⚠️  STREAM CONNECTION FAILED: 192.168.1.100
Reason: Reconnect failed after 5 attempts
Action: Executing full cleanup...
[GlobalHealthMonitor] Decoder stopped: 192.168.1.100
[GlobalHealthMonitor] Data flushed: 192.168.1.100
[GlobalHealthMonitor] ClientManager cleaned: 192.168.1.100
[GlobalHealthMonitor] Full cleanup completed: 192.168.1.100
```

## 测试建议

1. **孤儿流场景**
   ```bash
   # 启动流后，手动停止解码器但不清理 ClientManager
   # 等待 30 秒观察孤儿流检测是否触发
   ```

2. **断流重连**
   ```bash
   # 使用无效的 RTSP URL 启动流
   # 观察重连逻辑和最终的完整清理
   ```

3. **正常终止**
   ```bash
   # 使用 POST /api/terminate 终止流
   # 确认三层清理都执行
   ```

## 迁移影响

### 需要更新的文件

- ✅ **app/services/stream/service.py** - 删除 CleanupService 引用
- ✅ **app/routers/inspection.py** - 内联清理逻辑
- ✅ **app/services/health_monitor.py** - 增强孤儿流检测

### 已删除的文件

- ❌ **app/services/stream/cleanup.py**
- ❌ **app/services/stream/health_monitor.py**

### 文档需要更新

- API_MIGRATION_GUIDE.md - 移除 CleanupService 引用
- ARCHITECTURE_OVERVIEW.md - 更新架构图
- EXCEPTION_HANDLING.md - 更新清理流程

## 优势总结

| 维度 | 改进前 | 改进后 |
|-----|--------|--------|
| **模块数量** | 3 个（StreamHealthMonitor + CleanupService + GlobalHealthMonitor） | 1 个（GlobalHealthMonitor） |
| **职责清晰度** | ❌ 职责分散，相互依赖 | ✅ 职责集中，单一入口 |
| **孤儿流检测** | ❌ CleanupService 独立后台线程 | ✅ 集成到健康监控循环 |
| **完整清理** | ❌ 需要 API 层协调 | ✅ GlobalHealthMonitor 自动协调 |
| **可维护性** | ❌ 多个中间层，复杂 | ✅ 单一服务，简洁 |

## 相关文件

- [app/services/health_monitor.py](app/services/health_monitor.py) - 全局健康监控服务
- [app/routers/api.py](app/routers/api.py) - 统一 API（/api/start, /api/terminate）
- [app/routers/ai.py](app/routers/ai.py) - lifespan 初始化
- [API_MIGRATION_GUIDE.md](API_MIGRATION_GUIDE.md) - API 迁移指南
