# CleanSight 边界层异常处理设计

**设计日期**: 2026-02（v1.0）/ 2026-04-13 更新（v1.1）
**版本**: 1.1
**适用范围**: 实时 AI 视觉检测系统（10 路并发视频流 @ 30fps）

> **v1.1 变更**：部分业务路径（如 `StreamService.start_stream`）选择**退出 Layer 2**，首次失败直接返回、不重试，改由上层异步监控（如 `StreamHealthMonitor`）接管重连。Layer 2 不再强制覆盖所有同步业务调用。详见下文 [Layer 2 适用范围](#layer-2-适用范围与退出路径)。

---

## 一、设计原则

### 1.1 核心原则

- **业务代码保持纯净**: 只抛出异常，不捕获异常
- **框架边界层统一处理**: 在 4 个边界层捕获异常，集中管理重试逻辑
- **异常即协议**: 异常类型表达语义（`retryable`、`fatal`）
- **显式化决策**: DROP / RETRY / FATAL 三种处理动作

### 1.2 异常处理三原则

1. **分层捕获**: 在边界层捕获异常，不在业务逻辑中 try-catch
2. **失败可观测**: 所有异常记录到 Prometheus metrics
3. **快速失败**: 致命错误不重试，立即向上传播

---

## 二、四层边界架构

```
┌──────────────────────────────────────────────────────────────────┐
│ Layer 4: main()                                                  │
│   职责: 顶层 Fail-Fast                                            │
│   捕获: 所有未处理异常                                             │
│   动作: 记录 CRITICAL 日志 → sys.exit(1)                          │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
┌──────────────────────────────────────────────────────────────────┐
│ Layer 3: FastAPI Exception Handlers                             │
│   职责: HTTP 边界层                                               │
│   捕获: AppError + 子类、Exception                                │
│   动作: 转换为 HTTP 状态码（503/500）+ JSON 错误响应              │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
┌──────────────────────────────────────────────────────────────────┐
│ Layer 2: GuardedExecutor                                         │
│   职责: 框架边界层（重试执行器）                                  │
│   捕获: AppError                                                  │
│   动作: 决策 Action（DROP/RETRY/FATAL）→ 记录 metrics             │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
┌──────────────────────────────────────────────────────────────────┐
│ Layer 1: Worker.run()                                            │
│   职责: Worker 边界层                                             │
│   捕获: Exception                                                 │
│   动作: 记录错误日志 → 防止线程崩溃 → 继续处理下一个任务          │
└──────────────────────────────────────────────────────────────────┘
```

---

## 三、异常类型体系

### 3.1 异常层次结构

```python
AppError (基类)
├── FrameDrop (实时推理专用)
└── 服务级别异常 (5 个核心异常)
    ├── StreamConnectionError   # 流连接失败
    ├── FFmpegError             # FFmpeg 解码错误
    ├── DatabaseError           # 数据库错误
    ├── ModelInferenceError     # 模型推理错误
    └── PersistenceError        # 持久化错误（HLS/告警）
```

### 3.2 异常属性表

| 异常类型                  | retryable | fatal | 典型场景                           |
| ------------------------- | --------- | ----- | ---------------------------------- |
| `FrameDrop`               | ❌        | ❌    | 单帧解码失败、客户端已移除         |
| `StreamConnectionError`   | ✅        | ❌    | RTSP 连接超时、网络瞬时故障        |
| `FFmpegError`             | ❌        | ✅    | FFmpeg 进程崩溃、编码格式不支持    |
| `DatabaseError`           | ✅        | ❌    | 连接池耗尽、SQL 查询超时           |
| `ModelInferenceError`     | ❌        | ❌    | CUDA OOM、模型加载失败             |
| `PersistenceError`        | ✅        | ❌    | HLS 写入失败、磁盘临时满           |

### 3.3 FrameDrop 特殊处理

**设计目的**: 在 30fps 实时推理场景中，单帧失败允许安静丢弃，不影响系统运行。

**处理策略**:
- ✅ 返回 `None`（不抛出异常）
- ✅ 记录 `frame_drop_total` metric
- ❌ 不打印 ERROR 日志（只记录 DEBUG）
- ❌ 不重试（帧已丢失，无意义）

**适用场景**:
- 单帧解码失败
- 单帧推理超时
- 客户端已移除但帧仍在队列
- 单帧质量检查不通过

---

## 四、GuardedExecutor 重试策略

### Layer 2 适用范围与退出路径

**适用**：以下路径仍由 `GuardedExecutor` 统一重试——

- 数据库操作（DatabaseError，指数退避）
- 告警上报 / HLS 持久化（PersistenceError）
- 外部 API 调用（external_api 策略）
- 推理批次（ModelInferenceError，快速失败）

**已退出（由异步监控接管）**：

- `StreamService.start_stream()`：2026-04 起不再包 `executor.execute`。首次 `decoder.start()` 失败时：
  1. 日志记 WARNING；
  2. decoder 留在 `self.decoders` 字典中（v3 版重连前置注册）；
  3. `StreamHealthMonitor` 在下一周期检测到 `is_alive=False` 并按 5s × 最多 5 次发起异步重连。
- `StreamService.restart_stream()`：由健康监控周期调度，单次失败返回 False，下一周期再试——同样不经 RetryExecutor。

**判定原则**：

- 若调用方是同步 HTTP 请求且没有上层补救机制 → 走 Layer 2（有限重试后抛给 Layer 3）。
- 若调用方期望立即返回、且已有后台监控/调度器能周期性重试 → 退出 Layer 2，避免双重重试。

### 4.1 预定义策略

| 策略名称       | max_attempts | delay | backoff | 适用场景                 |
| -------------- | ------------ | ----- | ------- | ------------------------ |
| `stream`       | 5            | 3.0s  | ❌      | 流连接（网络波动）       |
| `database`     | 3            | 1.0s  | ✅ 2x   | 数据库操作（连接池恢复） |
| `external_api` | 3            | 2.0s  | ✅ 2x   | 外部 API 调用            |
| `inference`    | 2            | 1.0s  | ❌      | 模型推理（快速失败）     |
| `persistence`  | 3            | 1.0s  | ✅ 2x   | HLS 写入、告警上报       |

### 4.2 指数退避示例

```python
# database 策略：1s → 2s → 4s (max_delay = 60s)
delay = min(1.0 * (2.0 ** (attempts - 1)), 60.0)
```

### 4.3 Action 决策逻辑

```python
def _decide_action(exc: AppError, policy: ExecutionPolicy, attempts: int) -> Action:
    # 1. FrameDrop → DROP（安静丢弃）
    if isinstance(exc, FrameDrop):
        return Action.DROP

    # 2. fatal=True → FATAL（致命错误）
    if exc.fatal:
        return Action.FATAL

    # 3. retryable=True 且未超过次数 → RETRY
    if exc.retryable and attempts < policy.max_attempts:
        return Action.RETRY

    # 4. 其他 → FATAL（向上传播）
    return Action.FATAL
```

---

## 五、边界层实现示例

### 5.1 Layer 1: Worker 边界

```python
class TemporalWorker(threading.Thread):
    def run(self):
        while not self._stop_event.is_set():
            try:
                # 业务逻辑（纯净）
                result = self.temporal_queue.get(timeout=1.0)
                self.process_temporal_analysis(result)
            except queue.Empty:
                continue
            except Exception as e:
                # 边界捕获：防止线程崩溃
                logger.error(f"[Worker] Unhandled error: {e}", exc_info=True)
                # 继续处理下一个任务
```

### 5.2 Layer 2: GuardedExecutor 边界

```python
# 业务代码（纯净，只抛异常）
def connect_stream(url: str):
    if not validate_url(url):
        raise StreamConnectionError(url=url, client_id=self.client_id)
    # ... FFmpeg 连接逻辑

# 框架边界调用
executor = GuardedExecutor()
executor.execute(
    func=lambda: connect_stream(url),
    policy_name='stream'  # 自动重试 5 次，每次延迟 3 秒
)
```

### 5.3 Layer 3: FastAPI 边界

```python
@app.exception_handler(StreamConnectionError)
async def stream_error_handler(request: Request, exc: StreamConnectionError):
    logger.error(f"[HTTP] Stream error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=503,  # Service Unavailable
        content={
            "error": "Stream unavailable",
            "detail": str(exc),
            "client_id": exc.client_id,
        }
    )
```

### 5.4 Layer 4: main() 边界

```python
def main():
    try:
        import uvicorn
        uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
    except KeyboardInterrupt:
        logger.info("Received Ctrl+C, exiting...")
        sys.exit(0)
    except Exception as e:
        # 顶层边界：Fail-Fast
        logger.critical(f"[FATAL] Unhandled error: {e}", exc_info=True)
        sys.exit(1)
```

---

## 六、可观测性

### 6.1 Prometheus Metrics

```python
# 1. 重试计数（所有异常）
retry_total.labels(
    operation="stream",           # policy_name
    error_type="StreamConnectionError"
).inc()

# 2. 帧丢弃计数（FrameDrop 专用）
frame_drop_total.labels(
    client_id="192.168.1.100",
    reason="decode_failed"
).inc()

# 3. GPU OOM 计数（ModelInferenceError 专用）
gpu_oom_total.labels(
    model="bubble-best.pt"
).inc()
```

### 6.2 监控告警规则

```yaml
# Prometheus 告警规则示例
groups:
  - name: cleansight_alerts
    rules:
      # 帧丢失率过高
      - alert: HighFrameDropRate
        expr: rate(frame_drop_total[1m]) > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "帧丢失率超过 5 fps"

      # GPU OOM 频繁
      - alert: FrequentGpuOOM
        expr: increase(gpu_oom_total[5m]) > 3
        labels:
          severity: critical
        annotations:
          summary: "5 分钟内 GPU OOM 超过 3 次"
```

---

## 七、最佳实践

### 7.1 业务代码规范

✅ **正确写法**（纯净）:
```python
def query_task(task_id: int):
    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        raise DatabaseError(f"Task {task_id} not found", client_id=client_id)
    return task
```

❌ **错误写法**（在业务代码中重试）:
```python
def query_task(task_id: int):
    for attempt in range(3):  # ❌ 不要在业务代码中重试
        try:
            task = db.query(Task).filter_by(id=task_id).first()
            return task
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1)
```

### 7.2 调用规范

✅ **正确写法**（使用 GuardedExecutor）:
```python
executor = GuardedExecutor()
result = executor.execute(
    func=lambda: query_task(task_id),
    policy_name='database'
)
```

❌ **错误写法**（直接调用，无重试）:
```python
result = query_task(task_id)  # ❌ 失败后无重试
```

### 7.3 异常设计规范

✅ **正确写法**（使用自定义异常）:
```python
if disk_full:
    raise PersistenceError(
        message="Disk full",
        client_id=client_id,
        operation="hls_write",
        retryable=True  # 磁盘空间可能释放
    )
```

❌ **错误写法**（使用通用异常）:
```python
if disk_full:
    raise Exception("Disk full")  # ❌ 无法识别 retryable
```

---

## 八、常见场景处理

### 8.1 单帧失败（FrameDrop）

```python
# Executor 内部处理
try:
    frame = decode_frame(data)
except Exception:
    raise FrameDrop(client_id=client_id, reason="decode_failed")

# GuardedExecutor 返回 None，继续处理下一帧
result = executor.execute(func=infer_frame, policy_name='inference')
if result is None:
    continue  # 跳过丢失的帧
```

### 8.2 流连接失败（自动重试）

```python
# 业务代码
def start_ffmpeg(url: str):
    process = subprocess.Popen([...])
    if process.poll() is not None:
        raise StreamConnectionError(url=url, client_id=client_id)

# 框架边界（自动重试 5 次，每次间隔 3 秒）
executor.execute(
    func=lambda: start_ffmpeg(url),
    policy_name='stream'
)
```

### 8.3 数据库超时（指数退避）

```python
# 业务代码
def save_alarm(alarm: dict):
    try:
        db.session.add(Alarm(**alarm))
        db.session.commit()
    except OperationalError as e:
        raise DatabaseError("DB timeout", client_id=alarm["client_id"], retryable=True)

# 框架边界（指数退避：1s → 2s → 4s）
executor.execute(
    func=lambda: save_alarm(alarm),
    policy_name='database'
)
```

---

## 九、架构演进历史

### Phase 1: 初始设计（已完成）
- ✅ 定义 6 个核心异常类型
- ✅ 实现 GuardedExecutor 框架
- ✅ 集成 Prometheus metrics

### Phase 2: 清理向后兼容（已完成）
- ✅ 统一 `retryable` 命名（删除 `retry_able`）
- ✅ 删除动态方法替换（改用继承）
- ✅ 删除同步模式（统一为异步管道）
- ✅ 统一配置项命名

### Phase 3: 类型安全（已完成）
- ✅ 所有可选参数使用 `Optional[T]` 注解
- ✅ 实例属性添加类型注解
- ✅ 100% 测试覆盖率（33/33 通过）

---

## 十、参考资料

### 内部文档
- [异常处理实现](EXCEPTION_HANDLING.md) - 断线重连、超时清理机制
- [推理服务架构](kb/SERVICE_INFERENCE.md) - 异步管道设计
- [持久化服务](PERSISTENCE.md) - HLS Worker 异常处理

### 代码实现
- [app/utils/exceptions.py](../app/utils/exceptions.py) - 异常类型定义
- [app/utils/executor.py](../app/utils/executor.py) - GuardedExecutor 实现
- [app/main.py](../app/main.py) - FastAPI 异常处理器

### 测试用例
- [tests/test_exception_handling.py](../tests/test_exception_handling.py) - 完整测试套件
- [tests/test_boundary_layers.py](../tests/test_boundary_layers.py) - 边界层集成测试

---

**最后更新**: 2026-02-07
**维护者**: CleanSight Team
