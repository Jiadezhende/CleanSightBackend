# 推流断线重连与超时清理功能实现文档

> **版本**: 2.0
> **日期**: 2026-01-25
> **状态**: ✅ 已完成并测试

## 核心问题总结

### 问题根源

1. **两套重连机制冲突**
   - FFmpegDecoder 的内置 `auto_restart` 机制（最多5次）
   - StreamHealthMonitor 的统一重连机制（最多6次，每5秒）
   - 两者同时工作导致不可预测的行为和日志混乱

2. **启动失败无法检测**
   - `ClientQueues.latest_raw_timestamp` 初始化为 0
   - StreamHealthMonitor 跳过 `timestamp == 0` 的客户端
   - FFmpeg 启动失败（一帧未解码）时，StreamHealthMonitor 无法介入

3. **远程测试网络配置问题**
   - 远程服务器无法用外网IP连接自己的RTSP服务器
   - 测试脚本需要区分推流URL（外网IP）和拉流URL（localhost）

4. **日志刷屏问题**
   - `_try_restart()` 被无条件调用
   - 重连期间每次失败都输出WARNING日志

### 解决方案核心

1. **完全移除 FFmpegDecoder.auto_restart**
   - 删除 `auto_restart`、`max_restarts`、`restart_count` 属性
   - 删除 `_try_restart()` 方法
   - 所有重连由 StreamHealthMonitor 统一管理

2. **初始化 timestamp 为创建时间**
   - `latest_raw_timestamp` 初始化为 `time.time()`
   - StreamHealthMonitor 可以检测"启动失败"场景（5秒内无帧）

3. **规范化测试脚本**
   - 支持 `--server` 参数（默认 localhost）
   - 自动区分推流URL和拉流URL
   - 远程服务器使用 localhost 拉流

4. **清理日志**
   - stream ended 时输出 DEBUG 级别日志
   - 避免重连期间的WARNING刷屏

---

## 目录

1. [概述](#概述)
2. [问题背景](#问题背景)
3. [解决方案](#解决方案)
4. [架构设计](#架构设计)
5. [实现细节](#实现细节)
6. [文件清单](#文件清单)
7. [完整更改清单](#完整更改清单)
8. [测试指南](#测试指南)
9. [配置说明](#配置说明)

---

## 概述

本文档描述了 CleanSight 后端系统中**推流断线自动重连**和**超时清理**功能的完整实现。

### 核心功能

1. **断线检测**：监控 `latest_raw_timestamp`，5秒无新帧触发警告
2. **自动重连**：推流恢复后自动重启 FFmpegDecoder，无需手动调用 API
3. **智能重试**：每5秒尝试重连一次，最多6次（30秒）
4. **超时清理**：6次重连失败后自动清理所有资源
5. **优雅降级**：stop_stream API 采用 best-effort cleanup，永不返回404

### 用户体验提升

- ❌ **旧方案**：推流断开后需要人工手动重启，stop API 返回404错误
- ✅ **新方案**：推流恢复后自动检测并重连，无需人工干预，stop API 永远成功

---

## 问题背景

### 原始问题

用户在测试中发现以下问题：

1. **推流断开后无法自动重连**
   - FFmpeg 停止推流10秒后重新推流
   - 后端没有自动检测并恢复拉流
   - 需要手动调用 `start_rtsp_stream` API

2. **stop_stream API 返回404**
   - FFmpegDecoder 进程死亡后
   - 调用 stop_stream 返回404错误
   - 导致资源无法清理

3. **手动重连失败**
   - 调用 start_stream 时返回 "already started" 错误
   - 因为旧的 Decoder 记录未清理

### 用户需求

> "我希望在推流恢复后自动重连"

用户明确提出需要自动重连机制：
- 推流断开后3秒一次检测（实际实现为5秒）
- 1分钟后超时清理（实际实现为30秒，6次重试）
- 使用 `latest_raw_timestamp` 判断断流

---

## 解决方案

### 两阶段实现

#### Phase 1: 基础健康监控 + 优雅清理

**目标**：解决404错误，支持手动重连

**实现**：
1. 创建 `CleanupService`：统一、best-effort 清理服务
2. 修改 stop 接口：使用 CleanupService，永不抛异常
3. 创建 `StreamHealthMonitor`：后台监控 `latest_raw_timestamp`
4. 修改 `start_stream`：检测并清理死亡 decoder

**结果**：✅ 解决404问题，支持手动重连

#### Phase 2: 自动重连机制

**目标**：推流恢复后自动检测并重连

**实现**：
1. 增强 `StreamHealthMonitor`：
   - 添加重连状态机（正常 → 重连模式 → 成功/清理）
   - 每5秒尝试重连，最多6次
   - 检测新帧到达判定成功

2. 扩展 `StreamService`：
   - `get_stream_info()`：获取流配置（URL、FPS、协议）
   - `restart_stream()`：保留 ClientQueues，只重启 Decoder

**结果**：✅ 推流恢复后自动重连，无需人工干预

---

## 架构设计

### 组件架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      StreamService                          │
│  ┌────────────────────────────────────────────────────┐    │
│  │         StreamHealthMonitor (Background Thread)    │    │
│  │  - 每3秒检查 latest_raw_timestamp                  │    │
│  │  - 5秒无新帧 → 进入重连模式                        │    │
│  │  - 每5秒尝试重连（最多6次）                        │    │
│  │  - 30秒后清理资源                                  │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │              FFmpegDecoder (Per Client)            │    │
│  │  - Windows: _windows_reader_loop (blocking read)   │    │
│  │  - POSIX: on_stdout_ready (non-blocking read)      │    │
│  │  - 写入 ca_raw 和 ca_ready 队列                    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │              CleanupService (Singleton)            │    │
│  │  - cleanup_client(): 原子化清理                    │    │
│  │  - Best-effort: 永不抛异常                         │    │
│  │  - 清理 Decoder + InferenceManager                 │    │
│  └────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  ClientManager  │
                    │  - ClientQueues │
                    │  - ca_raw       │
                    │  - ca_ready     │
                    └─────────────────┘
```

### 重连状态机

```
┌──────────────┐
│ 正常监控      │  latest_raw_timestamp 持续更新
│ (Normal)     │
└──────┬───────┘
       │ 5秒无新帧
       ▼
┌──────────────┐
│ 重连模式      │  保存流配置，开始重连
│ (Reconnect)  │  attempt_count = 0
└──────┬───────┘
       │
       ├──────────────────────────────────┐
       │ 每5秒                             │
       ▼                                  │
┌──────────────┐                          │
│ 重连尝试      │ attempt_count++         │
│ restart_stream│                          │
└──────┬───────┘                          │
       │                                  │
       ├─── 检测到新帧? ───┐              │
       │                   │              │
       │ YES               │ NO           │
       ▼                   ▼              │
┌──────────────┐    ┌──────────────┐    │
│ 重连成功      │    │ 继续重试      │    │
│ 退出重连模式  │    │ (attempt < 6) │────┘
└──────────────┘    └──────┬───────┘
                           │ attempt >= 6
                           ▼
                    ┌──────────────┐
                    │ 重连失败      │
                    │ 清理资源      │
                    └──────────────┘
```

### 数据流时间线

**场景：推流断开10秒后恢复**

```
时间轴 (秒)    事件                             状态
─────────────────────────────────────────────────────────
00:00         推流开始                          NORMAL
              ca_raw queue: 0 → 300
              latest_raw_timestamp: 持续更新

00:15         FFmpeg 停止推流                   NORMAL
              latest_raw_timestamp: 冻结

00:20         5秒无新帧                         RECONNECT MODE
              → _enter_reconnect_mode()
              → 保存流配置 (URL, FPS, protocol)
              → attempt_count = 0

00:20         第1次重连尝试                     RECONNECTING
              → restart_stream()
              → 停止旧 Decoder
              → 启动新 Decoder (但推流未恢复)
              → 下一周期检测: 无新帧 → 失败

00:25         第2次重连尝试                     RECONNECTING
              → FFmpeg 已恢复推流 (at 00:25)
              → restart_stream()
              → Decoder 成功拉流
              → 下一周期检测: 有新帧! → 成功

00:28         检测到新帧                        SUCCESS
              → _exit_reconnect_mode(cleanup=False)
              → 恢复正常监控
              → latest_raw_timestamp: 继续更新

00:45         测试结束                          NORMAL
```

---

## 实现细节

### 1. CleanupService（统一清理服务）

**文件**: `app/services/stream/cleanup.py`

**设计原则**：
- **原子化**：单一职责，只负责清理
- **Best-effort**：永不抛异常，记录错误但继续执行
- **解耦**：不依赖业务逻辑，只清理资源

**核心方法**：

```python
def cleanup_client(self, client_id: str, reason: str = "manual") -> Dict[str, Any]:
    """清理客户端资源（尽力而为，永不抛异常）

    步骤：
    1. 清理 StreamService 中的 Decoder
    2. 清理 InferenceManager 中的资源
    3. 返回清理结果（含错误信息）

    Returns:
        {
            "decoder_cleaned": bool,
            "inference_cleaned": bool,
            "errors": List[str]
        }
    """
```

**关键特性**：
- 即使 Decoder 不存在也返回成功
- 捕获所有异常，记录但不抛出
- 详细的日志输出，便于调试

---

### 2. StreamHealthMonitor（健康监控服务）

**文件**: `app/services/stream/health_monitor.py`

#### Phase 1 功能

- 每3秒检查所有客户端的 `latest_raw_timestamp`
- 5秒无新帧：记录 SUSPECT 警告
- 60秒无新帧：触发清理（Phase 1 版本）

#### Phase 2 增强

**新增数据结构**：

```python
@dataclass
class ReconnectState:
    """重连状态"""
    client_id: str
    stream_url: str
    fps: int
    protocol: str
    attempt_count: int                        # 当前重连次数
    last_attempt_time: float                  # 上次尝试时间
    last_frame_time_before_disconnect: float  # 断流前的最后一帧时间
```

**新增配置参数**：

```python
self.reconnect_interval = 5.0           # 重连间隔（秒）
self.max_reconnect_attempts = 6         # 最大重连次数
self.reconnect_success_threshold = 5.0  # 成功判定时间（未使用，改为立即检测）
```

**核心方法**：

```python
def _check_client_health(self, client_id: str, cq, current_time: float):
    """增强版健康检查（支持重连）

    逻辑：
    1. 如果在重连模式 → 调用 _handle_reconnecting_client()
    2. 计算 time_since_last_frame
    3. 如果 5s ≤ time < 30s → 进入重连模式
    4. 如果 time ≥ 30s → 清理资源
    """

def _enter_reconnect_mode(self, client_id: str, cq, last_frame_time: float):
    """进入重连模式

    步骤：
    1. 从 StreamService 获取流配置
    2. 创建 ReconnectState
    3. 记录到 _reconnecting_clients 字典
    4. 立即尝试第1次重连
    """

def _handle_reconnecting_client(self, client_id: str, cq, current_time: float):
    """处理重连中的客户端

    逻辑：
    1. 检查是否有新帧到达（success判定）
    2. 检查是否到达重连间隔（5秒）
    3. 检查是否达到最大次数（6次）
    4. 调用 restart_stream() 尝试重连
    """

def _exit_reconnect_mode(self, client_id: str, cleanup: bool):
    """退出重连模式

    Args:
        cleanup: True=失败清理, False=成功恢复
    """
```

**重连成功判定**：

```python
# 检查是否有新帧到达
if cq.latest_raw_timestamp > state.last_frame_time_before_disconnect:
    logger.info(f"[StreamHealthMonitor] RECONNECT SUCCESS: {client_id}, "
                f"new frames detected (attempt {state.attempt_count})")
    self._exit_reconnect_mode(client_id, cleanup=False)
    return
```

---

### 3. StreamService（流管理服务）

**文件**: `app/services/stream/service.py`

#### Phase 1 修改

**修改 `start_stream()`**：

```python
def start_stream(self, client_id: str, stream_url: str, fps: int = 30, protocol: str = 'RTSP'):
    with self.lock:
        # 1. 检查是否存在旧的 Decoder
        old_dec = self.decoders.get(client_id)
        if old_dec:
            # 2. 如果 Decoder 已死亡 → 清理
            if not old_dec.is_alive():
                logger.warning(f"Detected dead decoder for {client_id}, cleaning up...")
                self._cleanup_dead_decoder_unsafe(client_id)
            else:
                # 3. 如果 Decoder 活着 → 不允许重复启动
                raise ValueError(f"Stream already started for {client_id}")

        # 4. 创建新 Decoder...
```

**新增 `_cleanup_dead_decoder_unsafe()`**：

```python
def _cleanup_dead_decoder_unsafe(self, client_id: str):
    """内部清理方法（必须持有锁）

    清理：
    1. 从 decoders 字典移除
    2. 从 selector unregister
    3. 从 metrics 移除

    注意：不清理 ClientQueues（保留用于重连）
    """
```

#### Phase 2 新增

**新增 `get_stream_info()`**：

```python
def get_stream_info(self, client_id: str) -> Optional[Dict[str, Any]]:
    """获取流配置信息（用于重连）

    Returns:
        {
            'url': str,       # RTSP URL
            'fps': int,       # 帧率
            'protocol': str   # 'RTSP' or 'RTMP'
        }
    """
    with self.lock:
        dec = self.decoders.get(client_id)
        if not dec:
            return None

        # 判断协议类型
        protocol = 'RTMP'
        if dec.protocol_opts and any('rtsp' in str(opt).lower() for opt in dec.protocol_opts):
            protocol = 'RTSP'

        return {
            'url': dec.stream_url,
            'fps': dec.fps,
            'protocol': protocol
        }
```

**新增 `restart_stream()`**：

```python
def restart_stream(self, client_id: str, stream_url: str, fps: int, protocol: str) -> bool:
    """重启流（用于自动重连）

    与 start_stream 的区别：
    - 不创建新的 ClientQueues（保留现有队列）
    - 只重启 FFmpegDecoder

    步骤：
    1. 停止并清理旧 Decoder
    2. 从 ClientManager 获取现有 ClientQueues
    3. 创建新 Decoder（使用现有 ClientQueues）
    4. 注册到 selector（如果需要）

    Returns:
        True=成功, False=失败（ClientQueues不存在）
    """
```

**关键差异**：

| 特性 | start_stream() | restart_stream() |
|------|----------------|------------------|
| ClientQueues | 创建新的 | 复用现有 |
| InferenceManager | 创建/更新 | 不操作 |
| 使用场景 | 首次启动 | 自动重连 |
| 调用来源 | API | HealthMonitor |

---

### 4. 修改 stop 接口

**文件**: `app/routers/inspection.py`

**修改前**：

```python
@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(...)):
    stream_service.stop_stream(client_id)  # 可能抛异常
    return {"status": "success"}
```

**修改后**：

```python
@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(...)):
    from app.services.stream.cleanup import cleanup_service
    result = cleanup_service.cleanup_client(client_id, reason="api_stop")
    return {
        "status": "success",
        "message": f"RTSP 流捕获已停止 for {client_id}",
        "cleanup_details": result
    }
```

**效果**：
- ✅ 永远返回200
- ✅ 即使 Decoder 不存在也成功
- ✅ 返回详细的清理结果

---

## 文件清单

### 新增文件

| 文件 | 描述 | 行数 |
|------|------|------|
| `app/services/stream/cleanup.py` | 统一清理服务 | ~100 |
| `app/services/stream/health_monitor.py` | 健康监控 + 自动重连 | ~250 |
| `integration_tests/test_reconnect_success.py` | 断线重连成功测试（规范版） | ~220 |
| `integration_tests/test_reconnect_timeout.py` | 超时清理测试（规范版） | ~250 |
| `integration_tests/TESTING_AUTO_RECONNECT.md` | 测试指南 | ~335 |
| `docs/STREAM_RECONNECT_IMPLEMENTATION.md` | 本文档 | ~800 |

### 修改文件

| 文件 | 修改内容 | 影响 |
|------|----------|------|
| `app/services/stream/service.py` | 1. 添加 `_cleanup_dead_decoder_unsafe()`<br>2. 修改 `start_stream()` 检测死亡 decoder<br>3. 添加 `get_stream_info()`<br>4. 添加 `restart_stream()`<br>5. 修改 `_ensure_health_monitor()` 传递 stream_service | +100行 |
| `app/services/stream/decoder.py` | 1. 删除 `auto_restart`、`max_restarts`、`restart_count` 属性<br>2. 删除 `_try_restart()` 方法<br>3. 修改 `on_stdout_ready()` 简化EOF处理 | -30行 |
| `app/services/client/queues.py` | 修改 `latest_raw_timestamp` 初始化为 `time.time()` | 1行 |
| `app/routers/inspection.py` | 1. 修改 `stop_rtsp_stream` 使用 CleanupService<br>2. 修改 `stop_stream` 使用 CleanupService | 修改2个函数 |

### 旧测试脚本（已废弃）

| 文件 | 状态 |
|------|------|
| `integration_tests/test_stream_disconnect_reconnect.py` | 已被 `test_reconnect_success.py` 替代 |
| `integration_tests/test_stream_reconnect_timeout.py` | 已被 `test_reconnect_timeout.py` 替代 |

### 文件依赖关系

```
app/routers/inspection.py
    └─> app/services/stream/cleanup.py (cleanup_service)
            ├─> app/services/stream/service.py (stream_service)
            └─> app/services/inference/core/manager.py (InferenceManager)

app/services/stream/service.py
    └─> app/services/stream/health_monitor.py (StreamHealthMonitor)
            ├─> app/services/client/manager.py (ClientManager)
            ├─> app/services/stream/cleanup.py (CleanupService)
            └─> app/services/stream/service.py (StreamService) [循环依赖，通过参数注入]
```

---

## 完整更改清单

### 核心修改

#### 1. 删除 FFmpegDecoder.auto_restart 机制

**文件**: `app/services/stream/decoder.py`

**删除内容**:
- `auto_restart` 参数（默认值 True）
- `max_restarts` 参数（默认值 5）
- `restart_count` 实例变量
- `_try_restart()` 方法（完整删除）

**修改内容**:
```python
# 修改前
def __init__(self, ..., auto_restart=True, max_restarts=5, ...):
    self.auto_restart = auto_restart
    self.max_restarts = max_restarts
    self.restart_count = 0

def on_stdout_ready(self):
    if not chunk:
        self._try_restart()  # 调用重启
        return

def _try_restart(self):
    # 复杂的重启逻辑...
    if not self.auto_restart or self.restart_count >= self.max_restarts:
        self.logger.warning("stream ended or crashed, not restarting")
        return
    # ...

# 修改后
def __init__(self, ..., client_queues=None):  # 删除 auto_restart 参数
    # 删除 auto_restart, max_restarts, restart_count

def on_stdout_ready(self):
    if not chunk:
        self.logger.debug("stream ended")  # 简化处理
        return
# 删除 _try_restart() 方法
```

**影响**: StreamHealthMonitor 统一管理所有重连

---

#### 2. 初始化 timestamp 支持启动失败检测

**文件**: `app/services/client/queues.py`

**修改**:
```python
# 修改前
self.latest_raw_timestamp: float = 0.0

# 修改后
self.latest_raw_timestamp: float = time.time()  # 初始化为创建时间
```

**原因**:
- 旧版本 `timestamp == 0` 导致 StreamHealthMonitor 跳过检查
- 新版本可以检测"启动失败"场景（5秒内无帧）

---

#### 3. StreamHealthMonitor 增强

**文件**: `app/services/stream/health_monitor.py`

**新增数据结构**:
```python
@dataclass
class ReconnectState:
    client_id: str
    stream_url: str
    fps: int
    protocol: str
    attempt_count: int
    last_attempt_time: float
    last_frame_time_before_disconnect: float
```

**新增方法**:
- `_enter_reconnect_mode()`: 进入重连模式
- `_handle_reconnecting_client()`: 处理重连逻辑
- `_exit_reconnect_mode()`: 退出重连模式

**修改**:
```python
# _check_client_health() 更新
def _check_client_health(self, client_id, cq, current_time):
    # 1. 检查是否在重连模式
    if client_id in self._reconnecting_clients:
        self._handle_reconnecting_client(...)
        return

    # 2. 更新跳过逻辑（防御性检查）
    if last_frame_time == 0:
        logger.warning(f"WARN: {client_id} has zero timestamp (unexpected)")
        return

    # 3. 进入重连模式（5秒无新帧）
    if 5.0 <= time_since_last_frame < 30.0:
        self._enter_reconnect_mode(...)
```

---

#### 4. StreamService 新增重连方法

**文件**: `app/services/stream/service.py`

**新增方法**:
```python
def get_stream_info(self, client_id: str) -> Optional[Dict[str, Any]]:
    """获取流配置信息（用于重连）"""
    # 返回 url, fps, protocol

def restart_stream(self, client_id: str, stream_url: str, fps: int, protocol: str) -> bool:
    """重启流（保留 ClientQueues）"""
    # 1. 停止旧 Decoder
    # 2. 获取现有 ClientQueues（不创建新的）
    # 3. 创建新 Decoder（使用现有 ClientQueues）
    # 4. 启动 Decoder
```

**关键差异**:
| 特性 | start_stream() | restart_stream() |
|------|----------------|------------------|
| ClientQueues | 创建新的 | 复用现有 |
| InferenceManager | 创建/更新 | 不操作 |
| 使用场景 | 首次启动 | 自动重连 |

---

#### 5. CleanupService 创建

**文件**: `app/services/stream/cleanup.py`（新增）

**设计原则**:
- **Best-effort**: 永不抛异常
- **原子化**: 单一职责，只负责清理
- **详细日志**: 记录所有清理步骤

**核心方法**:
```python
def cleanup_client(self, client_id: str, reason: str = "manual") -> Dict[str, Any]:
    """清理客户端资源（尽力而为，永不抛异常）

    Returns:
        {
            "decoder_cleaned": bool,
            "inference_cleaned": bool,
            "errors": List[str]
        }
    """
```

---

#### 6. stop API 优雅处理

**文件**: `app/routers/inspection.py`

**修改**:
```python
# 修改前
@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(...)):
    stream_service.stop_stream(client_id)  # 可能抛异常
    return {"status": "success"}

# 修改后
@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(...)):
    from app.services.stream.cleanup import cleanup_service
    result = cleanup_service.cleanup_client(client_id, reason="api_stop")
    return {
        "status": "success",
        "message": f"RTSP 流捕获已停止 for {client_id}",
        "cleanup_details": result
    }
```

**效果**: 永远返回 200，即使 Decoder 不存在

---

### 测试脚本规范化

#### 新增脚本

**1. test_reconnect_success.py**
```bash
# 本地测试
python integration_tests/test_reconnect_success.py --task_id 1

# 远程测试
python integration_tests/test_reconnect_success.py --task_id 1 --server 117.50.241.174
```

**特性**:
- 支持 `--server` 参数（默认 localhost）
- 自动区分推流URL（外网IP）和拉流URL（localhost）
- 测试断线重连成功场景

**2. test_reconnect_timeout.py**
```bash
# 本地测试
python integration_tests/test_reconnect_timeout.py --task_id 2

# 远程测试
python integration_tests/test_reconnect_timeout.py --task_id 2 --server 117.50.241.174
```

**特性**:
- 观察35秒（6次重连 + 余量）
- 验证自动清理
- 默认 task_id=2（避免冲突）

---

### 配置变更

无需修改配置文件，所有参数使用默认值：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 检查间隔 | 3秒 | StreamHealthMonitor 监控周期 |
| 断流阈值 | 5秒 | 触发重连模式 |
| 重连间隔 | 5秒 | 每次重连尝试间隔 |
| 最大重连次数 | 6次 | 连续失败后放弃 |
| 清理阈值 | 30秒 | 6次失败后清理 |

---

### 代码统计

| 类型 | 新增 | 删除 | 净增加 |
|------|------|------|--------|
| Python 代码 | ~850行 | ~50行 | ~800行 |
| 文档 | ~1000行 | 0行 | ~1000行 |
| 测试脚本 | ~500行 | 0行 | ~500行 |
| **总计** | **~2350行** | **~50行** | **~2300行** |

---

### 向后兼容性

✅ **完全兼容** - 所有修改都是内部实现，外部 API 未变化：
- `start_rtsp_stream` API：签名不变
- `stop_rtsp_stream` API：签名不变，行为改进（不再返回404）
- FFmpegDecoder：外部不直接使用，移除 `auto_restart` 不影响调用方

---

## 测试指南

### 测试环境要求

1. **后端服务运行**：`python main.py`
2. **数据库已配置**：SQLite 或 PostgreSQL
3. **测试视频存在**：`test/test_video.mp4`
4. **RTSP 服务器运行**：MediaMTX (端口8004)

### 测试场景1：断线重连成功

**脚本**: `integration_tests/test_stream_disconnect_reconnect.py`

**场景**：
- 推流15秒 → 停止10秒 → 恢复推流15秒

**预期**：
- 5秒后进入重连模式
- 第1次重连尝试失败（推流未恢复）
- 第2次重连尝试成功（推流已恢复）
- 推理服务无缝恢复

**运行**：

```bash
python integration_tests/test_stream_disconnect_reconnect.py --task_id 1
```

**预期日志**：

```
[StreamHealthMonitor] SUSPECT: rtsp.test.1, no frames for 5.2s
[StreamHealthMonitor] RECONNECT MODE: rtsp.test.1, will retry every 5.0s (max 6 times)
[StreamHealthMonitor] RECONNECT ATTEMPT 1/6: rtsp.test.1
[StreamService] Stream restarted for rtsp.test.1
[StreamHealthMonitor] Reconnect attempt 1 failed: no new frames
[StreamHealthMonitor] RECONNECT ATTEMPT 2/6: rtsp.test.1
[StreamService] Stream restarted for rtsp.test.1
[StreamHealthMonitor] RECONNECT SUCCESS: rtsp.test.1, new frames detected (attempt 2)
```

---

### 测试场景2：超时清理

**脚本**: `integration_tests/test_stream_reconnect_timeout.py`

**场景**：
- 推流15秒 → 停止推流（不恢复） → 观察35秒

**预期**：
- 5秒后进入重连模式
- 连续6次重连尝试（每5秒一次）
- 30秒后清理资源

**运行**：

```bash
python integration_tests/test_stream_reconnect_timeout.py --task_id 2
```

**预期日志**：

```
[StreamHealthMonitor] RECONNECT MODE: rtsp.test.timeout.2, will retry every 5.0s (max 6 times)
[StreamHealthMonitor] RECONNECT ATTEMPT 1/6: rtsp.test.timeout.2
[StreamHealthMonitor] Reconnect attempt 1 failed: no new frames
[StreamHealthMonitor] RECONNECT ATTEMPT 2/6: rtsp.test.timeout.2
[StreamHealthMonitor] Reconnect attempt 2 failed: no new frames
...
[StreamHealthMonitor] RECONNECT ATTEMPT 6/6: rtsp.test.timeout.2
[StreamHealthMonitor] Reconnect attempt 6 failed: no new frames
[StreamHealthMonitor] RECONNECT FAILED: rtsp.test.timeout.2, max attempts (6) reached
[CleanupService] Cleaning up rtsp.test.timeout.2 (reason: reconnect_timeout)
[CleanupService] Cleanup complete for rtsp.test.timeout.2 (success)
```

---

### 测试场景3：stop API 优雅处理

**手动测试**：

```bash
# 1. 启动推流
python integration_tests/test_stream_disconnect_reconnect.py --task_id 3

# 2. 在推流过程中，手动停止 FFmpeg (Ctrl+C)

# 3. 调用 stop API（应该成功，不返回404）
curl -X POST "http://localhost:8000/inspection/stop_rtsp_stream?client_id=rtsp.test.3"
```

**预期响应**：

```json
{
  "status": "success",
  "message": "RTSP 流捕获已停止 for rtsp.test.3",
  "cleanup_details": {
    "decoder_cleaned": true,
    "inference_cleaned": true,
    "errors": []
  }
}
```

**状态码**: `200 OK`（不是404）

---

## 配置说明

### 健康监控配置

**文件**: `app/services/stream/health_monitor.py:55-61`

```python
# 超时阈值（秒）
self.suspect_timeout = 5.0    # 5秒无新帧 → 进入重连模式
self.cleanup_timeout = 30.0   # 30秒后清理资源

# 重连参数
self.reconnect_interval = 5.0        # 重连间隔（秒）
self.max_reconnect_attempts = 6      # 最大重连次数
self.reconnect_success_threshold = 5.0  # 未使用（改为立即检测）
```

### 配置调整建议

| 参数 | 默认值 | 调整建议 |
|------|--------|----------|
| `suspect_timeout` | 5秒 | **不建议修改**：太短会误判，太长响应慢 |
| `reconnect_interval` | 5秒 | 可调整：网络差时可增加到10秒 |
| `max_reconnect_attempts` | 6次 | 可调整：总时长 = interval × attempts |
| `cleanup_timeout` | 30秒 | **自动计算**：interval × attempts |

**示例调整**（网络较差环境）：

```python
self.reconnect_interval = 10.0       # 10秒一次
self.max_reconnect_attempts = 6      # 6次尝试
self.cleanup_timeout = 60.0          # 60秒后清理
```

---

## 性能影响

### 资源开销

| 组件 | 线程数 | CPU占用 | 内存占用 |
|------|--------|---------|----------|
| StreamHealthMonitor | 1 | 可忽略（3秒周期） | ~1MB |
| CleanupService | 0（按需调用） | 可忽略 | 常驻内存 |

### 重连延迟

| 场景 | 检测延迟 | 首次重连 | 成功时间 |
|------|----------|----------|----------|
| 立即恢复（<5秒） | 0秒 | 不触发重连 | 立即 |
| 短暂断流（5-10秒） | 5秒 | 5秒 | 5-10秒 |
| 中等断流（10-15秒） | 5秒 | 5秒 | 10-15秒 |
| 长时间断流（>30秒） | 5秒 | 5秒 | 清理（不恢复） |

---

## 故障排查

### 问题1：重连日志未出现

**症状**：断流后没有看到 `RECONNECT MODE` 日志

**可能原因**：
1. StreamHealthMonitor 未启动
2. 检查间隔过长
3. ClientQueues 未正确创建

**排查步骤**：

```bash
# 1. 检查 HealthMonitor 是否启动
grep "StreamHealthMonitor.*Started" logs/app.log

# 2. 检查是否有定期检查日志
grep "Checking.*active clients" logs/app.log

# 3. 检查 ClientQueues 是否存在
grep "ClientQueues created" logs/app.log
```

---

### 问题2：重连失败返回 False

**症状**：日志显示 `restart_stream returned False`

**可能原因**：
1. ClientQueues 已被清理
2. RTSP 服务器未恢复
3. FFmpegDecoder 启动失败

**排查步骤**：

```bash
# 1. 检查 ClientQueues 状态
grep "Cannot restart stream: no ClientQueues" logs/app.log

# 2. 检查 RTSP 服务器
ffprobe rtsp://localhost:8004/live/<client_id>

# 3. 检查 FFmpeg 启动错误
grep "ffmpeg started\|ffmpeg error" logs/app.log
```

---

### 问题3：重连成功但推理未恢复

**症状**：重连成功但没有推理结果

**可能原因**：
1. InferenceManager 中的客户端已被移除
2. ca_ready 队列未被消费
3. Dispatcher 未刷新客户端列表

**排查步骤**：

```bash
# 1. 检查 InferenceManager 状态
curl http://localhost:8000/inspection/status

# 2. 检查队列消费日志
grep "ca_ready\|Dispatcher" logs/app.log

# 3. 检查 Dispatcher 刷新日志
grep "refresh_client_queues\|客户端列表已更新" logs/app.log
```

---

## 总结

### ✅ 已实现功能

1. **断线检测**：监控 `latest_raw_timestamp`，5秒触发
2. **自动重连**：推流恢复后自动重启 Decoder
3. **智能重试**：5秒×6次，平衡响应速度和资源占用
4. **超时清理**：30秒后自动释放所有资源
5. **优雅降级**：stop API 永不返回404

### 🎯 核心优势

1. **用户体验**：推流恢复后自动恢复，无需人工干预
2. **资源安全**：超时自动清理，防止资源泄漏
3. **健壮性**：Best-effort cleanup，永不抛异常
4. **可维护性**：清晰的模块划分，详细的日志输出
5. **可测试性**：完整的测试脚本和文档

### 📊 测试结果

| 场景 | 状态 | 备注 |
|------|------|------|
| 断线重连成功 | ✅ 通过 | 第2次尝试成功 |
| 超时清理 | ✅ 通过 | 30秒后自动清理 |
| stop API 优雅处理 | ✅ 通过 | 永不返回404 |
| Windows 兼容性 | ✅ 通过 | _windows_reader_loop 正常工作 |
| Ubuntu 兼容性 | ✅ 通过 | selector 轮询正常 |

### 🚀 下一步计划（可选）

1. **多客户端压力测试**：测试10+客户端同时断流重连
2. **网络抖动模拟**：测试频繁断连场景（1秒断1秒连）
3. **监控面板**：Web UI 显示重连状态和历史记录
4. **告警通知**：重连失败时发送邮件/企业微信通知
5. **配置热更新**：支持运行时修改重连参数

---

## 附录

### A. 日志示例（完整流程）

```log
# 推流开始
2026-01-24 22:57:05 [StreamService] stream started client=172.16.77.220 pid=89860
2026-01-24 22:57:05 [StreamHealthMonitor] Started (check_interval=3.0s)

# 正常推流
2026-01-24 22:57:06 [StreamService] [BACKPRESSURE] client=172.16.77.220: ca_ready=0/2700, ca_raw=0/2700
2026-01-24 22:57:10 [StreamService] [BACKPRESSURE] client=172.16.77.220: ca_ready=0/2700, ca_raw=100/2700

# 推流断开（FFmpeg 停止）
2026-01-24 22:57:20 [FFmpegDecoder.172.16.77.220] received 300 frames (raw=300, ready=146, dropped=0)

# 5秒后检测到断流
2026-01-24 22:57:26 [StreamHealthMonitor] SUSPECT: 172.16.77.220, no frames for 5.2s
2026-01-24 22:57:26 [StreamHealthMonitor] RECONNECT MODE: 172.16.77.220, will retry every 5.0s (max 6 times)

# 第1次重连尝试（失败）
2026-01-24 22:57:29 [StreamHealthMonitor] RECONNECT ATTEMPT 1/6: 172.16.77.220
2026-01-24 22:57:29 [StreamService] Dead decoder cleaned up: 172.16.77.220
2026-01-24 22:57:29 [FFmpegDecoder.172.16.77.220] ffmpeg started pid=35192
2026-01-24 22:57:29 [StreamService] Stream restarted for 172.16.77.220

# 第2次重连尝试（成功，FFmpeg 已恢复）
2026-01-24 22:57:35 [StreamHealthMonitor] RECONNECT ATTEMPT 2/6: 172.16.77.220
2026-01-24 22:57:35 [FFmpegDecoder.172.16.77.220] ffmpeg started pid=129536
2026-01-24 22:57:35 [StreamService] Stream restarted for 172.16.77.220

# 检测到新帧，重连成功
2026-01-24 22:57:37 [StreamService] [BACKPRESSURE] client=172.16.77.220: ca_ready=0/2700, ca_raw=114/2700
2026-01-24 22:57:38 [StreamHealthMonitor] RECONNECT SUCCESS: 172.16.77.220, new frames detected (attempt 2)

# 恢复正常推流
2026-01-24 22:57:40 [StreamService] [BACKPRESSURE] client=172.16.77.220: ca_ready=0/2700, ca_raw=214/2700

# 手动停止
2026-01-24 22:57:55 [CleanupService] Cleaning up 172.16.77.220 (reason: api_stop)
2026-01-24 22:57:55 [StreamService] stream stopped client=172.16.77.220
2026-01-24 22:57:55 [CleanupService] Cleanup complete for 172.16.77.220 (success)
```

### B. 相关文档链接

- [自动重连测试指南](../integration_tests/TESTING_AUTO_RECONNECT.md)
- [StreamHealthMonitor 源码](../app/services/stream/health_monitor.py)
- [CleanupService 源码](../app/services/stream/cleanup.py)
- [StreamService 源码](../app/services/stream/service.py)

---

**文档维护**：如需更新，请修改 `docs/STREAM_RECONNECT_IMPLEMENTATION.md` 并同步更新版本号和日期。
