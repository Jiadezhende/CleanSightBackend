# 容错设计

本文描述 CleanSightBackend 的异常处理分层、重试机制、熔断器、流断线重连和结算告警机制。

---

## 一、异常处理 4 个边界层

系统将异常处理责任分配到 4 个明确的边界层，业务代码只负责抛出，不负责捕获。

```
业务代码
  │ 抛出 AppError 子类
  ▼
L1: Worker.run() / guarded_run()
  │ 捕获线程崩溃，记录日志，按配置重启（最多 3 次，冷却 2 s）
  ▼
L2: GuardedExecutor.execute()
  │ 判断 retryable / fatal，执行重试或向上传播
  ▼
L3: FastAPI exception handlers
  │ 将 AppError 子类映射为 HTTP 状态码（404 / 400 / 409 / 500）
  ▼
L4: main() 顶层捕获
  │ 记录未预期异常，触发进程退出
```

**设计原则**：越靠内层的边界层越了解上下文，越靠外层越通用。业务代码不混入重试逻辑，重试策略集中在 L2 统一管理。

---

## 二、异常类层次

6 种业务异常类均继承自 `AppError`，通过 `retryable` 和 `fatal` 两个类属性描述语义：

| 异常类 | retryable | fatal | 典型场景 |
|-------|-----------|-------|---------|
| `FrameDrop` | False | False | 单帧解码 / 推理失败，静默丢弃 |
| `StreamConnectionError` | True | False | 网络瞬时中断，可重试 |
| `FFmpegError` | False | True | FFmpeg 进程崩溃，需重启解码器 |
| `DatabaseError` | True（可覆盖） | False | 连接池耗尽，重试后恢复 |
| `ModelInferenceError` | False | False | GPU OOM，重试无效；单路失败不影响其他路 |
| `PersistenceError` | True | False | 磁盘临时满，重试后恢复 |

HTTP 业务异常（`NotFoundError` / `ValidationError` / `ConflictError`）直接被 L3 捕获，不经过 GuardedExecutor。

实例级覆盖：可在构造时覆盖类属性，例如 `DatabaseError(retryable=False)` 用于特定的不可重试查询失败。

---

## 三、GuardedExecutor 重试机制

### 五种预定义策略

| 策略名 | max_attempts | delay | backoff |
|-------|-------------|-------|---------|
| `stream` | 5 | 3.0 s | 否（固定延迟） |
| `database` | 3 | 1.0 s | 是（2× 退避，上限 60 s） |
| `external_api` | 3 | 2.0 s | 是（2× 退避，上限 60 s） |
| `inference` | 2 | 1.0 s | 否 |
| `persistence` | 3 | 1.0 s | 是（2× 退避，上限 30 s） |

### 决策树

```
execute(func, policy_name) 捕获异常后：

  FrameDrop?
  └─ Action.DROP  → 静默丢弃，记录 frame_drop_total 指标

  fatal = True?
  └─ Action.FATAL → 直接向上传播

  retryable = True 且 attempts < max_attempts?
  └─ Action.RETRY → 等待 delay（指数退避时乘以 backoff_factor）→ 重试

  其他?
  └─ Action.FATAL → 向上传播
```

重试时记录 `retry_total[operation, error_type]` 指标。所有重试耗尽后仍失败则向上传播。

---

## 四、CircuitBreaker 熔断器

```
状态机：

CLOSED（正常）
  │ 连续失败 >= max_failures（默认 5）
  ▼
OPEN（熔断）
  │ reset_timeout（默认 60 s）后
  ▼
HALF_OPEN（试探）
  ├─ 下一次调用成功 → 回到 CLOSED，重置计数
  └─ 下一次调用失败 → 回到 OPEN，重置计时
```

`RetryExecutorWithCircuitBreaker` 组合了 `GuardedExecutor` 和 `CircuitBreaker`：熔断器打开时直接拒绝调用（不消耗重试次数），熔断器关闭时正常走重试逻辑。主要用于数据库操作，防止数据库短暂不可用期间产生大量重试风暴。

---

## 五、流断线重连

流断线重连由 `GlobalHealthMonitor` 异步接管，不使用 `GuardedExecutor`，避免双重重试机制产生竞态。

### 重连流程

```
GlobalHealthMonitor（1 s 心跳）
  │
  ├─ 检查所有 FFmpegDecoder 的心跳时间戳
  │   超过 heartbeat_timeout（默认 5 s）未更新
  │           ↓
  │   调用 StreamService.restart_stream(client_id, url, fps, protocol)
  │     1. 强制 kill 旧 FFmpeg 进程（消除 Phase-2 push 竞争）
  │     2. 清理 decoders 字典记录
  │     3. 创建新 FFmpegDecoder，注册 selector
  │     返回 bool（成功 / 失败）
  │           ↓
  │   失败 → 下一个心跳周期再次尝试
  │   最多重试 max_reconnect_attempts（默认 5）次
  │   仍失败 → cleanup_client（3 步完整清理）
  │
  └─ 检查孤儿任务（orphan_timeout = 30 s）
      超时未收到帧的任务 → cleanup_client
```

`restart_stream()` 返回 bool 而非抛出异常，健康监控根据返回值决定是否继续重试，逻辑与业务代码解耦。

### 与 start_stream 的关系

`start_stream()` 失败后不删除 `decoders` 字典中的记录（decoder 仍留在字典中），健康监控在下一个心跳发现该 decoder 已失活，触发 `restart_stream()`。这样消除了"API 重试 + 监控重试"的双重机制。

---

## 六、结算告警

实时告警在 `TemporalActor` 的 1 Hz Tick 中上升沿触发；结算告警在任务终止时由 `TemporalAnalyzer.finalize()` 收集，用于判断整个任务生命周期内的违规（例如：规定操作次数不足）。

### 触发时机

```
set_task(client_id, None) 或 remove_client(client_id)
  │
  └─ InferenceManager._set_task_locked() / ._remove_client_locked()
      └─ actor.finalize_and_stop()
          1. stop_event.set()
          2. thread.join(timeout=2.0)
          3. for analyzer in _analyzers:
               alarms.extend(analyzer.finalize())
          返回 List[AlarmInfo]
      └─ _persist_settlement_alarms(client_id, alarms)
          → PersistenceManager.persist_alarm(alarm, mode="SETTLEMENT")
```

### 典型实现：DebounceAnalyzer.finalize()

```python
def finalize(self) -> List[AlarmInfo]:
    if self._sm["bend_actions"] < self._required_actions:
        return [AlarmInfo(
            alarm_type="流程违规",
            level="warning",
            message=f"操作次数不足：{self._sm['bend_actions']}/{self._required_actions}"
        )]
    return []
```

结算告警只在任务级别判断，不依赖实时状态机，因此即使任务因异常中止，仍能正确收集。

---

## 七、任务最大时长保护

`GlobalHealthMonitor` 支持配置 `task_max_duration`（默认 1800 s）。超过该时长的任务会被自动 `cleanup_client`，防止因客户端异常断连导致任务永久挂起、资源不释放。设为 0 表示不限制时长。
