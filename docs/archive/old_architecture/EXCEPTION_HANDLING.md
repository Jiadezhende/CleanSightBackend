# CleanSight 异常处理机制

本文档介绍 CleanSight 系统的异常处理、断线重连和资源清理机制。

## 断线重连机制

### StreamHealthMonitor 工作原理

**位置**: `app/services/stream/health_monitor.py`

**重连状态机**:
```
正常状态 --[5秒无帧]--> 嫌疑状态 --[再等5秒]--> 重连状态
```

**配置参数** (`config/stream_config.yaml`):
```yaml
health_monitor:
  check_interval: 5.0          # 每5秒检查一次
  heartbeat_timeout: 10.0      # 10秒无帧判定断流
  restart_delay: 3.0           # 重试间隔3秒
  max_restart_attempts: 5      # 最多重试5次
  restart_window: 60.0         # 60秒内最多重试5次
```

**重连流程**:
1. 检测到10秒无帧 → 标记为断流
2. 尝试重启 FFmpeg 进程
3. 等待3秒后检查是否恢复
4. 最多重试5次，成功条件：连续10秒收到帧
5. 5次重试失败 → 清理客户端资源

### 重连测试

```bash
# 测试自动重连
python integration_tests/test_reconnect_success.py --task_id 1

# 测试超时清理
python integration_tests/test_reconnect_timeout.py --task_id 1
```

---

## Timeout 清理机制

### CleanupService 工作流程

**位置**: `app/services/stream/cleanup.py`

**配置参数**:
```yaml
cleanup:
  check_interval: 30.0    # 每30秒检查一次
  orphan_timeout: 90.0    # 90秒无活动判定为孤儿流
```

**清理流程**:
1. 定期检查所有客户端的最后活动时间
2. 超过90秒无帧 → 标记为超时
3. 执行原子化清理（每步独立try-except）:
   - 清理 FFmpeg 解码器
   - 清理推理队列
   - 清理持久化任务
   - 更新客户端状态为空闲
4. 返回清理结果报告

---

## 其他容错处理

### 1. 背压控制（Backpressure）

队列达到90%满时自动丢帧，防止内存溢出。

```yaml
decoder:
  backpressure_ratio: 0.90  # 队列90%满时开始丢帧
```

### 2. 孤儿流检测

检测有队列但无解码器的流，自动清理。

### 3. WebSocket 错误处理

- 自动检测连接断开（ConnectionResetError, BrokenPipeError）
- 优雅关闭连接
- 清理客户端资源

### 4. FFmpeg 进程失败恢复

- 检查进程退出码
- 记录 stderr 日志
- 自动重启或清理

---

## 相关文档

- [断线重连实现](STREAM_RECONNECT_IMPLEMENTATION.md) - 详细技术实现
- [整体架构](kb/ARCHITECTURE_OVERVIEW.md) - 服务架构设计

**最后更新**: 2026-01-30
