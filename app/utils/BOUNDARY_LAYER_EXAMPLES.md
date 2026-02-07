# 边界层异常处理示例

本文档展示如何在 CleanSight 项目中正确使用边界层异常处理模式。

## 核心原则

1. **业务代码保持纯净**：只抛异常，不捕获异常
2. **边界层统一捕获**：在 4 个边界层统一处理异常
3. **框架层处理重试**：使用 GuardedExecutor 在框架层实现重试逻辑

---

## 示例 1: StreamService（流服务）

### ❌ 错误做法（业务代码被污染）

```python
class StreamService:
    def start_stream(self, url: str):
        """❌ 业务代码中有 try/except，破坏业务语义"""
        try:
            self._validate_url(url)
            self._connect_rtsp(url)
            self._start_ffmpeg(url)
        except Exception as e:
            logger.error(f"Failed to start stream: {e}")
            raise StreamConnectionError(url=url)
```

**问题**:
- 业务代码被 try/except 污染
- 异常处理策略分散
- 重试逻辑隐藏在业务代码中

---

### ✅ 正确做法（边界层异常处理）

```python
from app.utils import GuardedExecutor, StreamConnectionError, log_call

class StreamService:
    def __init__(self):
        # 创建执行器（框架边界层）
        self.executor = GuardedExecutor()

    @log_call(level=logging.INFO)  # ✅ 日志装饰器
    def start_stream(self, url: str):
        """
        服务层方法（调用框架边界层）

        框架边界层处理重试逻辑，业务代码保持纯净
        """
        return self.executor.execute(
            func=lambda: self._start_ffmpeg(url),
            policy_name='stream'  # 固定延迟 3 秒，最多 5 次
        )

    def _start_ffmpeg(self, url: str):
        """
        业务代码（纯净，只抛异常）

        职责：
        1. 验证 URL
        2. 连接 RTSP
        3. 启动 FFmpeg
        4. 如果失败，抛出 StreamConnectionError
        """
        # 验证 URL
        if not self._validate_url(url):
            raise StreamConnectionError(
                url=url,
                client_id=self.client_id
            )

        # 连接 RTSP
        if not self._connect_rtsp(url):
            raise StreamConnectionError(
                url=url,
                client_id=self.client_id
            )

        # 启动 FFmpeg
        if not self._launch_ffmpeg(url):
            raise FFmpegError(
                "Failed to launch FFmpeg",
                client_id=self.client_id,
                exit_code=1
            )

        return self.client_id
```

**优势**:
- 业务代码 `_start_ffmpeg()` 完全纯净，只抛异常
- 重试逻辑在框架边界层统一管理 (`GuardedExecutor`)
- 控制流清晰，易于调试

---

## 示例 2: DatabaseService（数据库服务）

### ❌ 错误做法

```python
def get_task(task_id: int):
    """❌ 业务代码中有 try/except + 手动重试"""
    for attempt in range(3):
        try:
            session = SessionLocal()
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise DatabaseError(f"Task {task_id} not found")
            return task
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1.0 * (2 ** attempt))
```

**问题**:
- 业务代码混杂重试逻辑
- 熔断器保护缺失
- 难以统一调整策略

---

### ✅ 正确做法（边界层 + 熔断器）

```python
from app.utils import GuardedExecutorWithCircuitBreaker, DatabaseError, log_call

class DatabaseService:
    def __init__(self):
        # 创建执行器（重试 + 熔断器）
        self.executor = GuardedExecutorWithCircuitBreaker(
            policy_name='database',  # 指数退避，最多 3 次
            breaker_name='database',
            max_failures=5,
            reset_timeout=60.0
        )

    @log_call(level=logging.DEBUG)
    def get_task(self, task_id: int):
        """
        服务层方法（调用框架边界层）

        框架边界层处理：
        1. 重试逻辑（指数退避）
        2. 熔断器保护（连续失败 5 次后打开）
        """
        return self.executor.execute(
            func=lambda: self._query_task(task_id)
        )

    def _query_task(self, task_id: int):
        """
        业务代码（纯净，只抛异常）

        职责：
        1. 查询数据库
        2. 如果失败，抛出 DatabaseError
        """
        session = SessionLocal()
        task = session.query(Task).filter_by(id=task_id).first()

        if not task:
            raise DatabaseError(
                f"Task {task_id} not found",
                retry_able=True
            )

        return task
```

**优势**:
- 业务代码 `_query_task()` 完全纯净
- 重试 + 熔断器在框架边界层统一管理
- 连续失败 5 次后自动打开熔断器，保护数据库

---

## 示例 3: InferenceWorker（推理工作线程）

### ❌ 错误做法

```python
class InferenceWorker(Thread):
    def run(self):
        """❌ Worker 入口没有异常捕获，线程可能崩溃"""
        while not self._stop_event.is_set():
            self._process_batch()  # 如果抛出异常，线程崩溃

    def _process_batch(self):
        """❌ 业务代码中有 try/except"""
        try:
            frames = self.queue.get(timeout=1.0)
            results = self.model.infer(frames)
            self.output_queue.put(results)
        except Exception as e:
            logger.error(f"Inference failed: {e}")
            raise ModelInferenceError(str(e))
```

**问题**:
- Worker 入口没有异常捕获，线程可能崩溃
- 业务代码混杂异常处理

---

### ✅ 正确做法（边界层 1: Worker 入口）

```python
from app.utils import ModelInferenceError, log_call, timing

class InferenceWorker(Thread):
    def run(self):
        """
        Worker 入口（边界层 1: 线程入口）

        职责：
        1. 捕获所有未处理异常
        2. 防止线程崩溃
        3. 记录日志
        """
        try:
            # 业务逻辑（纯净，只抛异常）
            while not self._stop_event.is_set():
                self._process_batch()

        except Exception as e:
            # 边界层捕获所有异常
            logger.error(
                f"[Worker-{self.name}] Uncaught exception in worker: {e}",
                exc_info=True
            )
            # 不重新抛出，防止线程崩溃

    @timing(threshold_ms=1000.0, warn_on_slow=True)
    def _process_batch(self):
        """
        业务代码（纯净，只抛异常）

        职责：
        1. 从队列获取帧
        2. 调用模型推理
        3. 将结果放入输出队列
        4. 如果失败，抛出 ModelInferenceError
        """
        # 获取帧
        frames = self.queue.get(timeout=1.0)

        # 调用模型推理
        results = self.model.infer(frames)
        if results is None:
            raise ModelInferenceError(
                "Model inference returned None",
                client_id=self.client_id,
                model_name="bubble_detection"
            )

        # 放入输出队列
        self.output_queue.put(results)
```

**优势**:
- Worker 入口捕获所有异常，防止线程崩溃（边界层 1）
- 业务代码 `_process_batch()` 完全纯净
- 性能计时装饰器自动记录推理耗时

---

## 示例 4: API 路由（HTTP 边界层）

### ❌ 错误做法

```python
@router.get("/load_task/{task_id}")
async def load_task(task_id: int):
    """❌ API 路由中有 try/except"""
    try:
        task = query_task(task_id)
        client_id = start_stream(task.stream_url)
        return {"client_id": client_id}
    except DatabaseError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    except StreamConnectionError:
        raise HTTPException(status_code=503, detail="Stream unavailable")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

**问题**:
- API 路由被 try/except 污染
- 异常到 HTTP 状态码的映射分散在各处

---

### ✅ 正确做法（边界层 3: HTTP 全局处理器）

#### 1. API 路由（纯净，不捕获异常）

```python
@router.get("/load_task/{task_id}")
async def load_task(task_id: int):
    """
    API 路由（纯净，只抛异常）

    异常由全局处理器捕获（边界层 3）
    """
    # 查询任务（可能抛出 DatabaseError）
    task = query_task(task_id)

    # 启动流（可能抛出 StreamConnectionError）
    client_id = start_stream(task.stream_url)

    # 返回结果（异常自动被全局处理器捕获）
    return {"client_id": client_id, "task_id": task_id}
```

#### 2. 全局异常处理器（边界层 3）

```python
# app/main.py

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.utils import (
    AppError,
    StreamConnectionError,
    DatabaseError,
    ModelInferenceError,
)

app = FastAPI()

@app.exception_handler(StreamConnectionError)
async def stream_error_handler(request: Request, exc: StreamConnectionError):
    """流连接错误处理器（边界层 3）"""
    logger.error(
        f"Stream connection error: {exc}",
        exc_info=True,
        extra={"client_id": exc.client_id, "url": request.url}
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "Stream unavailable",
            "detail": str(exc),
            "client_id": exc.client_id
        }
    )

@app.exception_handler(DatabaseError)
async def database_error_handler(request: Request, exc: DatabaseError):
    """数据库错误处理器（边界层 3）"""
    logger.error(
        f"Database error: {exc}",
        exc_info=True,
        extra={"client_id": exc.client_id}
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "Database unavailable",
            "detail": str(exc)
        }
    )

@app.exception_handler(ModelInferenceError)
async def inference_error_handler(request: Request, exc: ModelInferenceError):
    """推理错误处理器（边界层 3）"""
    logger.error(
        f"Model inference error: {exc}",
        exc_info=True,
        extra={"client_id": exc.client_id}
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Inference failed",
            "detail": str(exc)
        }
    )

@app.exception_handler(AppError)
async def cleansight_exception_handler(request: Request, exc: AppError):
    """CleanSight 通用异常处理器（边界层 3）"""
    logger.error(
        f"CleanSight exception: {exc}",
        exc_info=True,
        extra={"client_id": exc.client_id}
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal error",
            "detail": str(exc)
        }
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """兜底异常处理器（边界层 3）"""
    logger.error(
        f"Uncaught exception: {exc}",
        exc_info=True,
        extra={"url": str(request.url)}
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": "An unexpected error occurred"
        }
    )
```

**优势**:
- API 路由完全纯净，不包含任何 try/except
- 异常到 HTTP 状态码的映射集中在全局处理器
- 易于维护和调整异常处理策略

---

## 示例 5: main() 入口（顶层 Fail-Fast）

### ✅ 正确做法（边界层 4）

```python
# app/main.py

import sys
import logging

logger = logging.getLogger(__name__)

def main():
    """
    应用入口（边界层 4: 顶层 Fail-Fast）

    职责：
    1. 捕获所有未处理异常
    2. 记录日志
    3. 优雅退出
    """
    try:
        # 初始化服务
        logger.info("Starting CleanSight Backend...")

        # 启动 FastAPI 应用
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)

    except KeyboardInterrupt:
        logger.info("Received shutdown signal, exiting...")

    except Exception as e:
        # 顶层边界捕获所有未处理异常
        logger.critical(
            f"Fatal error in main: {e}",
            exc_info=True
        )
        # Fail-Fast: 记录日志后退出
        sys.exit(1)

if __name__ == "__main__":
    main()
```

**优势**:
- 顶层入口捕获所有未处理异常
- Fail-Fast 策略：记录日志后优雅退出
- 防止应用在未知状态下继续运行

---

## 总结

### 4 个边界层职责

| 边界层 | 位置 | 职责 |
|--------|------|------|
| **边界层 1** | Worker.run() | 防止线程崩溃 |
| **边界层 2** | GuardedExecutor | 统一重试逻辑 |
| **边界层 3** | FastAPI 全局处理器 | HTTP 异常转换 |
| **边界层 4** | main() | 顶层 Fail-Fast |

### 业务代码准则

1. **只抛异常，不捕获异常**
2. **不使用 @retry 和 @circuit_breaker 装饰器**
3. **仅使用 @log_call 和 @timing 日志装饰器**
4. **保持业务语义纯净**

### 框架边界层准则

1. **GuardedExecutor**: 统一重试策略
2. **CircuitBreaker**: 保护下游服务
3. **全局异常处理器**: 统一 HTTP 错误响应
4. **Worker.run()**: 防止线程崩溃
5. **main()**: Fail-Fast 策略

---

## 参考

- [app/utils/executor.py](app/utils/executor.py): GuardedExecutor、CircuitBreaker 实现
- [app/utils/exceptions.py](app/utils/exceptions.py): 自定义异常类
- [app/utils/decorators.py](app/utils/decorators.py): 日志装饰器
- [边界层异常处理架构文档](C:\Users\31399\.claude\plans\boundary-layer-exception-handling.md)
