# CleanSight Utils - 边界层异常处理指南

CleanSight 工具模块基于**边界层异常处理架构**，专为并发 <15 的小规模系统设计。

## 核心原则

1. **业务代码保持纯净**：只抛异常，不捕获异常
2. **框架边界层处理重试**：使用 RetryExecutor 统一管理重试逻辑
3. **异常捕获在 4 个边界层**：
   - 边界层 1: Worker.run() - 防止线程崩溃
   - 边界层 2: RetryExecutor - 统一重试逻辑
   - 边界层 3: FastAPI 全局处理器 - HTTP 异常转换
   - 边界层 4: main() - 顶层 Fail-Fast

---

## 快速开始

### 1. 导入模块

```python
from app.utils import (
    # 异常类
    StreamConnectionError,
    FFmpegError,
    DatabaseError,
    ModelInferenceError,
    PersistenceError,
    # 框架边界层（推荐使用）
    RetryExecutor,
    CircuitBreaker,
    RetryExecutorWithCircuitBreaker,
    # 日志装饰器
    log_call,
    timing,
    # 上下文管理
    ClientContext,
    set_client_id,
    get_client_id,
)
```

---

## 核心功能

### 1. 异常类

5 个核心异常类，所有异常携带 `client_id` 和 `retry_able` 标识：

```python
# 流连接错误（可重试）
raise StreamConnectionError(url="rtsp://192.168.1.100:8554/live", client_id="client_001")

# FFmpeg 错误（不可重试）
raise FFmpegError("FFmpeg not found", exit_code=1)

# 数据库错误（可重试）
raise DatabaseError("Connection timeout", retry_able=True)

# 模型推理错误
raise ModelInferenceError("CUDA OOM", model_name="bubble_detection", is_cuda_error=True)

# 持久化错误（可重试）
raise PersistenceError("HLS write failed", operation="hls_write")
```

---

### 2. RetryExecutor（框架边界层）

**职责**：在框架层统一处理重试逻辑，业务代码保持纯净

**预定义策略**：
- `'stream'`: 固定延迟 3 秒，最多 5 次（适用于流连接）
- `'database'`: 指数退避，最多 3 次（适用于数据库）
- `'external_api'`: 指数退避，最多 3 次（适用于外部 API）
- `'inference'`: 固定延迟 1 秒，最多 2 次（适用于模型推理）
- `'persistence'`: 指数退避，最多 3 次（适用于持久化）

**示例**：

```python
from app.utils import RetryExecutor, StreamConnectionError

class StreamService:
    def __init__(self):
        # 创建执行器（框架边界层）
        self.executor = RetryExecutor()

    def start_stream(self, url: str):
        """服务层方法（调用框架边界层）"""
        return self.executor.execute(
            func=lambda: self._connect(url),
            policy_name='stream'  # 固定延迟 3 秒，最多 5 次
        )

    def _connect(self, url: str):
        """业务代码（纯净，只抛异常）"""
        if not self._validate_url(url):
            raise StreamConnectionError(url=url, client_id=self.client_id)
        # ... 连接逻辑
```

**关键点**：
- ✅ 业务代码 `_connect()` 完全纯净，只抛异常
- ✅ 重试逻辑在框架边界层 `RetryExecutor.execute()` 统一管理
- ✅ 控制流清晰，易于调试

---

### 3. CircuitBreaker（框架边界层）

**职责**：保护下游服务（数据库、外部 API），连续失败 N 次后打开熔断器

**示例**：

```python
from app.utils import CircuitBreaker, DatabaseError

class DatabaseService:
    def __init__(self):
        # 创建熔断器
        self.breaker = CircuitBreaker(
            name='database',
            max_failures=5,
            reset_timeout=60.0
        )

    def query_task(self, task_id: int):
        """服务层方法（通过熔断器调用）"""
        return self.breaker.call(
            func=lambda: self._query(task_id)
        )

    def _query(self, task_id: int):
        """业务代码（纯净，只抛异常）"""
        task = self.db.query(Task).filter_by(id=task_id).first()
        if not task:
            raise DatabaseError(f"Task {task_id} not found", retry_able=True)
        return task
```

---

### 4. RetryExecutorWithCircuitBreaker（组合）

**职责**：组合重试 + 熔断器，适用于数据库和外部 API

**示例**：

```python
from app.utils import RetryExecutorWithCircuitBreaker, DatabaseError

class DatabaseService:
    def __init__(self):
        # 创建组合执行器（重试 + 熔断器）
        self.executor = RetryExecutorWithCircuitBreaker(
            policy_name='database',  # 指数退避，最多 3 次
            breaker_name='database',
            max_failures=5,
            reset_timeout=60.0
        )

    def get_task(self, task_id: int):
        """服务层方法（框架边界层处理重试和熔断器）"""
        return self.executor.execute(
            func=lambda: self._query_task(task_id)
        )

    def _query_task(self, task_id: int):
        """业务代码（纯净，只抛异常）"""
        task = self.db.query(Task).filter_by(id=task_id).first()
        if not task:
            raise DatabaseError(f"Task {task_id} not found", retry_able=True)
        return task
```

---

### 5. 日志装饰器

**仅保留日志装饰器**：`@log_call` 和 `@timing`

#### 5.1 `@log_call` - 自动进入/退出日志

```python
from app.utils import log_call

@log_call(level=logging.INFO, log_args=True)
def process_frame(client_id: str, frame: np.ndarray):
    # ... 处理帧
    pass

# 日志输出：
# [ENTER] process_frame (client_id=192.168.1.100, args=...)
# [EXIT] process_frame (elapsed=45.23ms, client_id=192.168.1.100)
```

**高频操作优化**（生产环境自动跳过）：

```python
@log_call(skip_in_production=True)
def infer_batch(frames):
    # 生产环境（DEBUG_MODE=false）自动跳过日志
    pass
```

**参数说明**：
- `level`: 日志级别（默认 INFO）
- `log_args`: 是否记录参数（自动清洗大对象）
- `log_result`: 是否记录返回值
- `log_exceptions`: 是否记录异常（包含 traceback）
- `skip_in_production`: 生产环境是否跳过

#### 5.2 `@timing` - 性能计时

```python
from app.utils import timing

@timing(threshold_ms=1000.0, warn_on_slow=True)
def infer_batch(frames):
    # ... 推理
    pass

# 如果执行超过 1000ms：
# [SLOW] infer_batch took 1523.45ms (threshold=1000.0ms, client_id=...)
```

**参数说明**：
- `threshold_ms`: 警告阈值（毫秒）
- `warn_on_slow`: 超过阈值时是否发出警告
- `log_always`: 是否总是记录（默认仅超过阈值时记录）

---

## 完整示例

### 示例 1: StreamService（流服务）

```python
from app.utils import RetryExecutor, StreamConnectionError, log_call

class StreamService:
    def __init__(self):
        self.executor = RetryExecutor()

    @log_call(level=logging.INFO)
    def start_stream(self, url: str):
        """服务层方法（调用框架边界层）"""
        return self.executor.execute(
            func=lambda: self._start_ffmpeg(url),
            policy_name='stream'  # 固定延迟 3 秒，最多 5 次
        )

    def _start_ffmpeg(self, url: str):
        """业务代码（纯净，只抛异常）"""
        if not self._validate_url(url):
            raise StreamConnectionError(url=url, client_id=self.client_id)
        # ... FFmpeg 启动逻辑
```

### 示例 2: DatabaseService（数据库服务）

```python
from app.utils import RetryExecutorWithCircuitBreaker, DatabaseError, log_call

class DatabaseService:
    def __init__(self):
        # 创建执行器（重试 + 熔断器）
        self.executor = RetryExecutorWithCircuitBreaker(
            policy_name='database',
            breaker_name='database',
            max_failures=5,
            reset_timeout=60.0
        )

    @log_call(level=logging.DEBUG)
    def get_task(self, task_id: int):
        """服务层方法（框架边界层处理重试和熔断器）"""
        return self.executor.execute(
            func=lambda: self._query_task(task_id)
        )

    def _query_task(self, task_id: int):
        """业务代码（纯净，只抛异常）"""
        task = self.db.query(Task).filter_by(id=task_id).first()
        if not task:
            raise DatabaseError(f"Task {task_id} not found", retry_able=True)
        return task
```

### 示例 3: InferenceWorker（推理工作线程）

```python
from app.utils import ModelInferenceError, timing

class InferenceWorker(Thread):
    def run(self):
        """Worker 入口（边界层 1: 线程入口）"""
        try:
            # 业务逻辑（纯净，只抛异常）
            while not self._stop_event.is_set():
                self._process_batch()

        except Exception as e:
            # 边界层捕获所有异常，防止线程崩溃
            logger.error(
                f"[Worker-{self.name}] Uncaught exception: {e}",
                exc_info=True
            )

    @timing(threshold_ms=1000.0, warn_on_slow=True)
    def _process_batch(self):
        """业务代码（纯净，只抛异常）"""
        frames = self.queue.get(timeout=1.0)
        results = self.model.infer(frames)

        if results is None:
            raise ModelInferenceError(
                "Model inference returned None",
                client_id=self.client_id,
                model_name="bubble_detection"
            )

        self.output_queue.put(results)
```

### 示例 4: API 路由（HTTP 边界层）

#### 4.1 API 路由（纯净，不捕获异常）

```python
@router.get("/load_task/{task_id}")
async def load_task(task_id: int):
    """API 路由（纯净，只抛异常）"""
    # 查询任务（可能抛出 DatabaseError）
    task = query_task(task_id)

    # 启动流（可能抛出 StreamConnectionError）
    client_id = start_stream(task.stream_url)

    # 返回结果（异常自动被全局处理器捕获）
    return {"client_id": client_id, "task_id": task_id}
```

#### 4.2 全局异常处理器（边界层 3）

```python
# app/main.py

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.utils import StreamConnectionError, DatabaseError

app = FastAPI()

@app.exception_handler(StreamConnectionError)
async def stream_error_handler(request: Request, exc: StreamConnectionError):
    """流连接错误处理器（边界层 3）"""
    logger.error(f"Stream error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=503,
        content={"error": "Stream unavailable", "detail": str(exc)}
    )

@app.exception_handler(DatabaseError)
async def database_error_handler(request: Request, exc: DatabaseError):
    """数据库错误处理器（边界层 3）"""
    logger.error(f"Database error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=503,
        content={"error": "Database unavailable", "detail": str(exc)}
    )
```

---

## ⚠️ 废弃的功能

### 废弃的装饰器

以下装饰器已废弃，请使用框架边界层代替：

- ❌ `@retry` → ✅ 使用 `RetryExecutor`
- ❌ `@circuit_breaker` → ✅ 使用 `CircuitBreaker`

**为什么废弃**：
1. 装饰器会污染业务代码
2. 重试逻辑隐藏在装饰器中，难以调试
3. 违背边界层异常处理原则

**迁移指南**：

```python
# ❌ 旧方案（装饰器，已废弃）
@retry(max_attempts=5, delay=3.0)
def connect_stream(url: str):
    # ... 连接逻辑

# ✅ 新方案（框架边界层）
executor = RetryExecutor()

def connect_stream(url: str):
    """业务代码（纯净，只抛异常）"""
    if error:
        raise StreamConnectionError(url=url)
    # ... 连接逻辑

# 服务层调用（框架边界层）
executor.execute(
    func=lambda: connect_stream(url),
    policy_name='stream'
)
```

---

## 环境变量配置

```bash
# .env.dev (开发环境)
LOG_LEVEL=DEBUG
LOG_FORMAT=text         # 使用 colorlog
DEBUG_MODE=true         # 启用所有装饰器

# .env.prod (生产环境)
LOG_LEVEL=INFO
LOG_FORMAT=json         # 结构化日志
DEBUG_MODE=false        # 跳过高频装饰器
```

---

## 设计原则

- **业务代码纯净**: 只抛异常，不捕获异常
- **框架边界层**: 统一处理重试和熔断器
- **4 个边界层**: Worker.run(), RetryExecutor, FastAPI handlers, main()
- **零配置**: 参数硬编码，不需要额外配置文件
- **性能友好**: 高频操作条件激活（DEBUG 模式）
- **实用优先**: 避免过度设计，适用于并发 <15 的小规模系统

---

## 常见问题

### Q1: 为什么不使用 @retry 装饰器？

**A**: 装饰器会污染业务代码，违背边界层异常处理原则。使用 `RetryExecutor` 可以让业务代码保持纯净，重试逻辑在框架边界层统一管理。

### Q2: 如何自定义重试策略？

**A**: 创建自定义 `RetryPolicy` 并传递给 `RetryExecutor`：

```python
from app.utils import RetryExecutor, RetryPolicy

custom_policy = RetryPolicy(
    max_attempts=10,
    delay=5.0,
    backoff=True,
    backoff_factor=3.0,
    max_delay=120.0
)

executor = RetryExecutor(custom_policies={'custom': custom_policy})
executor.execute(func=my_func, policy_name='custom')
```

### Q3: 熔断器何时使用？

**A**: 仅用于数据库连接和外部 API，其他场景用 `RetryExecutor` 即可。

### Q4: 业务代码可以使用 try/except 吗？

**A**: **不推荐**。业务代码应该保持纯净，只抛异常。异常捕获应该在 4 个边界层进行。

---

## 参考文档

- [边界层异常处理示例](app/utils/BOUNDARY_LAYER_EXAMPLES.md) - 完整示例和最佳实践
- [实施计划](C:\Users\31399\.claude\plans\boundary-layer-exception-handling.md) - 架构设计和迁移步骤
- [异常类文档](app/utils/exceptions.py) - 5 个核心异常类
- [执行器文档](app/utils/executor.py) - RetryExecutor 和 CircuitBreaker 实现
