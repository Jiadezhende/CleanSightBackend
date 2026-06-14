# StreamService 边界层异常处理迁移计划

> **迁移日期**: 2026-06-14

## 一、迁移目标

将 StreamService 从传统的 try/except 异常处理模式迁移到**边界层异常处理架构**，实现：

1. **业务代码保持纯净**：只抛异常，不捕获异常
2. **框架边界层处理重试**：使用 RetryExecutor 统一管理重试逻辑
3. **异常捕获在边界层**：Worker.run(), RetryExecutor, FastAPI handlers

---

## 二、当前状态分析

### 2.1 文件结构

```
app/services/stream/
├── __init__.py
├── service.py           # 主服务（513 行）- 核心迁移目标
├── decoder.py           # FFmpeg 解码器（270 行）- 核心迁移目标
├── health_monitor.py    # 健康监控 - 需要迁移
├── cleanup.py           # 清理服务 - 需要迁移
└── config.py            # 配置加载 - 保持不变
```

### 2.2 异常处理现状

通过搜索发现：
- **service.py**: 19 处 `except Exception`
- **decoder.py**: 10 处 `except Exception`
- **health_monitor.py**: 3 处 `except Exception`
- **cleanup.py**: 4 处 `except Exception`

**总计**: 37 处泛化异常处理需要迁移

### 2.3 典型问题模式

#### ❌ 问题 1: 泛化异常处理

```python
# service.py 当前代码
def start_stream(self, client_id: str, stream_url: str):
    try:
        # ... 业务逻辑
        decoder = FFmpegDecoder(...)
        decoder.start()
    except Exception as e:
        logger.error(f"Failed to start stream: {e}")
        raise RuntimeError(f"Failed to start stream for {client_id}")
```

**问题**：
- 捕获所有异常，丢失了异常类型信息
- 业务代码被 try/except 污染
- 手动重新抛出异常，增加复杂度

#### ❌ 问题 2: 手动重试逻辑

```python
# health_monitor.py 当前代码
def _restart_stream(self, client_id: str):
    for attempt in range(3):
        try:
            self.stream_service.start_stream(client_id, url)
            break
        except Exception as e:
            if attempt == 2:
                logger.error(f"Failed to restart after 3 attempts")
                raise
            time.sleep(5)
```

**问题**：
- 重试逻辑分散在各处
- 硬编码重试次数和延迟
- 难以统一调整策略

#### ❌ 问题 3: 缺乏异常分类

```python
# decoder.py 当前代码
def start(self):
    try:
        self.process = subprocess.Popen(...)
    except Exception as e:
        logger.error(f"FFmpeg start failed: {e}")
        raise
```

**问题**：
- 所有异常都是 `Exception`，无法区分错误类型
- 无法根据错误类型决定是否重试
- API 层无法返回准确的 HTTP 状态码

---

## 三、迁移策略

### 3.1 核心原则

1. **业务代码纯净化**
   - 删除所有 try/except 块
   - 业务方法只抛出 CleanSight 异常
   - 不进行任何异常捕获

2. **框架边界层重试**
   - 在服务层调用业务方法时使用 RetryExecutor
   - 重试逻辑统一在框架层管理
   - 根据异常 retry_able 标志决定是否重试

3. **分层责任明确**
   - **业务层**（_private 方法）：纯净逻辑，只抛异常
   - **服务层**（public 方法）：调用 RetryExecutor
   - **API 层**：异常由全局处理器捕获

### 3.2 迁移步骤

#### 阶段 1: 准备工作（已完成 ✅）

- [x] 创建 5 个核心异常类
- [x] 实现 RetryExecutor 框架层
- [x] 实现 FastAPI 全局异常处理器
- [x] 编写测试验证边界层

#### 阶段 2: service.py 迁移

**迁移计划**：

1. **在 `__init__` 中创建 RetryExecutor**
   ```python
   def __init__(self):
       self.executor = RetryExecutor()
       # ... 其他初始化
   ```

2. **重构 `start_stream()` 方法**
   - 将当前的业务逻辑提取到 `_start_stream_impl()` 私有方法
   - `_start_stream_impl()` 只抛异常，不捕获
   - `start_stream()` 通过 RetryExecutor 调用 `_start_stream_impl()`

3. **移除所有 try/except 块**
   - 删除泛化的 `except Exception`
   - 替换为具体的 CleanSight 异常抛出

4. **异常分类映射**
   ```python
   # RTSP/RTMP 连接失败 → StreamConnectionError
   # FFmpeg 启动失败 → FFmpegError
   # 配置加载失败 → DatabaseError (如果涉及数据库)
   ```

#### 阶段 3: decoder.py 迁移

**迁移计划**：

1. **FFmpegDecoder.start() 重构**
   - 提取业务逻辑到 `_launch_ffmpeg()` 私有方法
   - `_launch_ffmpeg()` 只抛 FFmpegError
   - `start()` 通过 RetryExecutor 调用

2. **FFmpegDecoder.run() 边界层保护**
   - 添加边界层 1 异常捕获（防止线程崩溃）
   - 业务逻辑 `_process_frames()` 保持纯净

#### 阶段 4: health_monitor.py 迁移

**迁移计划**：

1. **删除手动重试逻辑**
   - `_restart_stream()` 中的 for 循环删除
   - 使用 RetryExecutor 替代

2. **边界层保护**
   - `run()` 方法添加边界层 1 异常捕获

#### 阶段 5: cleanup.py 迁移

**迁移计划**：

1. **清理方法纯净化**
   - 删除 try/except 块
   - 失败时抛出 PersistenceError

---

## 四、详细迁移示例

### 4.1 service.py - start_stream() 方法迁移

#### ❌ 迁移前（当前代码）

```python
class StreamService:
    def start_stream(self, client_id: str, stream_url: str, fps: int = 30, protocol: str = 'RTMP'):
        with self.lock:
            # 检查是否已有解码器
            if client_id in self.decoders:
                existing = self.decoders[client_id]
                if not existing.is_alive():
                    logger.warning(f"Removing dead decoder for {client_id}")
                    self._cleanup_dead_decoder_unsafe(client_id)
                else:
                    raise RuntimeError(f"stream {client_id} already started")

            try:
                # 创建解码器
                decoder = FFmpegDecoder(
                    client_id=client_id,
                    stream_url=stream_url,
                    # ... 其他参数
                )

                # 启动解码器
                decoder.start()

                # 注册到健康监控
                if self.health_monitor:
                    self.health_monitor.register_stream(client_id, stream_url)

                self.decoders[client_id] = decoder

            except Exception as e:
                logger.error(f"Failed to start stream for {client_id}: {e}")
                raise RuntimeError(f"Failed to start stream for {client_id}")
```

#### ✅ 迁移后（边界层架构）

```python
from app.utils import (
    RetryExecutor,
    StreamConnectionError,
    FFmpegError,
    log_call,
)

class StreamService:
    def __init__(self):
        # 创建 RetryExecutor（框架边界层）
        self.executor = RetryExecutor()
        self.decoders: Dict[str, FFmpegDecoder] = {}
        self.lock = threading.Lock()
        # ... 其他初始化

    @log_call(level=logging.INFO, log_args=True)
    def start_stream(self, client_id: str, stream_url: str, fps: int = 30, protocol: str = 'RTMP'):
        """
        服务层方法（调用框架边界层）

        通过 RetryExecutor 执行流启动，自动处理重试逻辑
        """
        return self.executor.execute(
            func=lambda: self._start_stream_impl(client_id, stream_url, fps, protocol),
            policy_name='stream'  # 固定延迟 3 秒，最多 5 次
        )

    def _start_stream_impl(self, client_id: str, stream_url: str, fps: int, protocol: str):
        """
        业务代码（纯净，只抛异常）

        职责：
        1. 检查解码器状态
        2. 创建并启动解码器
        3. 注册到健康监控
        4. 如果失败，抛出 StreamConnectionError 或 FFmpegError
        """
        with self.lock:
            # 检查是否已有解码器
            if client_id in self.decoders:
                existing = self.decoders[client_id]
                if not existing.is_alive():
                    logger.warning(f"[{client_id}] Removing dead decoder before restart")
                    self._cleanup_dead_decoder_unsafe(client_id)
                else:
                    # 业务逻辑异常：流已启动
                    raise StreamConnectionError(
                        f"Stream already started",
                        url=stream_url,
                        client_id=client_id
                    )

            logger.info(f"[{client_id}] Starting stream: protocol={protocol}, url={stream_url}")

            # 创建或获取 ClientQueues
            client_queues = self._get_or_create_client_queues(client_id)
            if not client_queues:
                raise StreamConnectionError(
                    "Failed to create client queues",
                    url=stream_url,
                    client_id=client_id
                )

            # 创建解码器（纯净调用，可能抛出 FFmpegError）
            decoder = self._create_decoder(
                client_id=client_id,
                stream_url=stream_url,
                fps=fps,
                protocol=protocol,
                client_queues=client_queues
            )

            # 启动解码器（纯净调用，可能抛出 FFmpegError）
            self._launch_decoder(decoder)

            # 注册到健康监控
            self._register_to_health_monitor(client_id, stream_url)

            # 保存解码器
            self.decoders[client_id] = decoder

            logger.info(f"[{client_id}] Stream started successfully")

    def _create_decoder(self, client_id: str, stream_url: str, fps: int,
                        protocol: str, client_queues) -> FFmpegDecoder:
        """
        业务代码：创建解码器（纯净，只抛异常）
        """
        # 验证 URL
        if not self._validate_stream_url(stream_url, protocol):
            raise StreamConnectionError(
                f"Invalid stream URL for protocol {protocol}",
                url=stream_url,
                client_id=client_id
            )

        # 创建解码器实例
        decoder = FFmpegDecoder(
            client_id=client_id,
            stream_url=stream_url,
            fps=fps,
            protocol=protocol,
            output_queue=client_queues.frame_queue,
            selector=self.sel,
        )

        return decoder

    def _launch_decoder(self, decoder: FFmpegDecoder):
        """
        业务代码：启动解码器（纯净，只抛异常）

        可能抛出：
        - FFmpegError: FFmpeg 启动失败
        """
        # FFmpegDecoder.start() 会抛出 FFmpegError
        decoder.start()

    def _register_to_health_monitor(self, client_id: str, stream_url: str):
        """
        业务代码：注册到健康监控（纯净，只抛异常）
        """
        if self.health_monitor:
            # 确保健康监控启动
            self._ensure_health_monitor()
            self.health_monitor.register_stream(client_id, stream_url)

    def _validate_stream_url(self, url: str, protocol: str) -> bool:
        """
        业务代码：验证流 URL（纯净，只抛异常）
        """
        if protocol == 'RTSP' and not url.startswith('rtsp://'):
            return False
        if protocol == 'RTMP' and not url.startswith('rtmp://'):
            return False
        return True

    def _get_or_create_client_queues(self, client_id: str):
        """
        业务代码：获取或创建客户端队列（纯净，只抛异常）
        """
        if client_manager is None:
            return None

        # 从配置文件读取参数
        inference_fps = self._get_inference_fps()
        resize_width, resize_height = self._get_frame_size()
        ca_maxlen, ca_segment_len = self._get_ca_params()

        client_queues = client_manager.get_client(
            client_id,
            resize_width=resize_width,
            resize_height=resize_height,
            inference_fps=inference_fps,
            ca_maxlen=ca_maxlen,
            ca_segment_len=ca_segment_len
        )

        return client_queues
```

**关键变化**：

1. ✅ **业务代码纯净化**
   - 所有私有方法（`_xxx_impl`, `_create_decoder`, `_launch_decoder` 等）只抛异常
   - 不包含任何 try/except 块
   - 业务语义清晰

2. ✅ **框架边界层处理重试**
   - `start_stream()` 通过 `RetryExecutor` 调用业务方法
   - 重试逻辑统一在框架层管理
   - 固定延迟 3 秒，最多重试 5 次

3. ✅ **异常分类准确**
   - URL 验证失败 → `StreamConnectionError`
   - FFmpeg 启动失败 → `FFmpegError`
   - 流已启动 → `StreamConnectionError`

4. ✅ **日志装饰器**
   - 使用 `@log_call` 自动记录进入/退出日志
   - 自动提取 `client_id` 参数

---

### 4.2 decoder.py - FFmpegDecoder 迁移

#### ❌ 迁移前（当前代码）

```python
class FFmpegDecoder(threading.Thread):
    def start(self):
        """启动 FFmpeg 解码器"""
        try:
            # 构建 FFmpeg 命令
            cmd = self._build_ffmpeg_command()

            # 启动进程
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )

            # 启动线程
            super().start()

        except Exception as e:
            logger.error(f"FFmpeg start failed for {self.client_id}: {e}")
            raise

    def run(self):
        """线程主循环"""
        try:
            while not self._stop_event.is_set():
                self._process_frames()
        except Exception as e:
            logger.error(f"FFmpeg decoder crashed for {self.client_id}: {e}")
            raise
```

#### ✅ 迁移后（边界层架构）

```python
from app.utils import FFmpegError, log_call, timing

class FFmpegDecoder(threading.Thread):
    def __init__(self, ...):
        super().__init__(name=f"FFmpegDecoder-{client_id}", daemon=True)
        self.client_id = client_id
        self.stream_url = stream_url
        # ... 其他初始化

    @log_call(level=logging.INFO)
    def start(self):
        """
        启动 FFmpeg 解码器（业务代码，纯净）

        职责：
        1. 构建 FFmpeg 命令
        2. 启动 FFmpeg 进程
        3. 启动解码线程
        4. 如果失败，抛出 FFmpegError
        """
        # 验证 FFmpeg 可执行文件
        if not self._validate_ffmpeg():
            raise FFmpegError(
                "FFmpeg executable not found",
                client_id=self.client_id
            )

        # 构建命令
        cmd = self._build_ffmpeg_command()

        # 启动进程（可能失败）
        self.process = self._launch_ffmpeg_process(cmd)

        # 启动线程
        super().start()

        logger.info(f"[{self.client_id}] FFmpeg decoder started (PID={self.process.pid})")

    def _validate_ffmpeg(self) -> bool:
        """验证 FFmpeg 可执行文件是否存在"""
        import shutil
        return shutil.which('ffmpeg') is not None

    def _launch_ffmpeg_process(self, cmd: list) -> subprocess.Popen:
        """
        业务代码：启动 FFmpeg 进程（纯净，只抛异常）

        可能抛出：
        - FFmpegError: 进程启动失败
        """
        # 启动进程（如果失败会抛出 OSError）
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )

        # 验证进程是否成功启动
        if process.poll() is not None:
            # 进程已退出，读取错误信息
            _, stderr = process.communicate()
            raise FFmpegError(
                f"FFmpeg process exited immediately",
                client_id=self.client_id,
                exit_code=process.returncode,
                stderr=stderr.decode('utf-8', errors='ignore')
            )

        return process

    def run(self):
        """
        线程主循环（边界层 1: Worker 入口）

        职责：
        1. 捕获所有未处理异常
        2. 防止线程崩溃
        3. 记录日志
        """
        try:
            logger.info(f"[{self.client_id}] FFmpeg decoder thread started")

            # 业务逻辑（纯净，只抛异常）
            while not self._stop_event.is_set():
                self._process_frames()

            logger.info(f"[{self.client_id}] FFmpeg decoder stopped normally")

        except Exception as e:
            # 边界层捕获所有异常
            logger.error(
                f"[BoundaryLayer1] FFmpeg decoder thread crashed for {self.client_id}: {e}",
                exc_info=True
            )
            # 不重新抛出，防止线程崩溃

    @timing(threshold_ms=100.0, warn_on_slow=True)
    def _process_frames(self):
        """
        业务代码：处理帧（纯净，只抛异常）

        可能抛出：
        - FFmpegError: 读取帧失败、解码失败
        """
        # 读取帧数据
        frame_data = self._read_frame_data()
        if not frame_data:
            raise FFmpegError(
                "Failed to read frame data",
                client_id=self.client_id
            )

        # 解码帧
        frame = self._decode_frame(frame_data)
        if frame is None:
            raise FFmpegError(
                "Failed to decode frame",
                client_id=self.client_id
            )

        # 放入队列
        self._enqueue_frame(frame)

    def _read_frame_data(self) -> bytes:
        """读取帧数据（纯净，只抛异常）"""
        # ... 实现
        pass

    def _decode_frame(self, data: bytes):
        """解码帧（纯净，只抛异常）"""
        # ... 实现
        pass

    def _enqueue_frame(self, frame):
        """将帧放入队列（纯净，只抛异常）"""
        # ... 实现
        pass
```

**关键变化**：

1. ✅ **边界层 1 保护**
   - `run()` 方法捕获所有异常，防止线程崩溃
   - 业务逻辑 `_process_frames()` 保持纯净

2. ✅ **业务代码纯净化**
   - 所有私有方法只抛 FFmpegError
   - 不包含任何 try/except 块

3. ✅ **性能监控**
   - 使用 `@timing` 装饰器监控帧处理性能
   - 超过 100ms 自动发出警告

---

### 4.3 health_monitor.py - 健康监控迁移

#### ❌ 迁移前（当前代码）

```python
class HealthMonitor(threading.Thread):
    def _restart_stream(self, client_id: str, url: str):
        """重启流（手动重试逻辑）"""
        for attempt in range(3):
            try:
                logger.info(f"Restarting stream for {client_id}, attempt {attempt+1}/3")
                self.stream_service.start_stream(client_id, url)
                logger.info(f"Stream {client_id} restarted successfully")
                break
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Failed to restart {client_id} after 3 attempts: {e}")
                    raise
                logger.warning(f"Restart attempt {attempt+1} failed: {e}")
                time.sleep(5)
```

#### ✅ 迁移后（边界层架构）

```python
from app.utils import RetryExecutor, StreamConnectionError

class HealthMonitor(threading.Thread):
    def __init__(self, stream_service, check_interval: int = 10):
        super().__init__(name="HealthMonitor", daemon=True)
        self.stream_service = stream_service
        self.check_interval = check_interval
        self._stop_event = threading.Event()

        # 创建 RetryExecutor（框架边界层）
        self.executor = RetryExecutor()

        # 监控的流
        self.monitored_streams: Dict[str, str] = {}  # client_id -> url
        self.lock = threading.Lock()

    def run(self):
        """
        线程主循环（边界层 1: Worker 入口）

        职责：
        1. 捕获所有未处理异常
        2. 防止线程崩溃
        3. 记录日志
        """
        try:
            logger.info("[HealthMonitor] Started")

            # 业务逻辑（纯净，只抛异常）
            while not self._stop_event.is_set():
                self._check_all_streams()
                time.sleep(self.check_interval)

            logger.info("[HealthMonitor] Stopped normally")

        except Exception as e:
            # 边界层捕获所有异常
            logger.error(
                f"[BoundaryLayer1] HealthMonitor thread crashed: {e}",
                exc_info=True
            )
            # 不重新抛出，防止线程崩溃

    def _check_all_streams(self):
        """
        业务代码：检查所有流（纯净，只抛异常）
        """
        with self.lock:
            for client_id, url in list(self.monitored_streams.items()):
                if self._is_stream_dead(client_id):
                    logger.warning(f"[{client_id}] Stream is dead, restarting...")
                    self._restart_stream(client_id, url)

    def _is_stream_dead(self, client_id: str) -> bool:
        """检查流是否已死（纯净，只抛异常）"""
        decoder = self.stream_service.decoders.get(client_id)
        if not decoder:
            return True
        return not decoder.is_alive()

    def _restart_stream(self, client_id: str, url: str):
        """
        服务层方法（调用框架边界层）

        通过 RetryExecutor 重启流，自动处理重试逻辑
        """
        # 先停止旧流
        self.stream_service.stop_stream(client_id)

        # 通过 RetryExecutor 重启（固定延迟 3 秒，最多 5 次）
        self.executor.execute(
            func=lambda: self.stream_service._start_stream_impl(client_id, url, 30, 'RTMP'),
            policy_name='stream',
            on_retry=lambda attempt, exc: logger.warning(
                f"[{client_id}] Restart attempt {attempt} failed: {exc}"
            )
        )

        logger.info(f"[{client_id}] Stream restarted successfully")

    def register_stream(self, client_id: str, url: str):
        """注册流到健康监控"""
        with self.lock:
            self.monitored_streams[client_id] = url
        logger.info(f"[{client_id}] Registered to health monitor")

    def unregister_stream(self, client_id: str):
        """从健康监控注销流"""
        with self.lock:
            self.monitored_streams.pop(client_id, None)
        logger.info(f"[{client_id}] Unregistered from health monitor")
```

**关键变化**：

1. ✅ **删除手动重试逻辑**
   - 删除了 `for attempt in range(3)` 循环
   - 使用 `RetryExecutor` 统一处理重试

2. ✅ **边界层 1 保护**
   - `run()` 方法捕获所有异常
   - 业务逻辑保持纯净

3. ✅ **重试回调**
   - 使用 `on_retry` 参数记录重试日志
   - 更清晰的重试追踪

---

## 五、迁移检查清单

### 5.1 service.py

- [ ] 在 `__init__` 中创建 RetryExecutor
- [ ] 重构 `start_stream()` 方法
  - [ ] 提取业务逻辑到 `_start_stream_impl()`
  - [ ] 通过 RetryExecutor 调用
  - [ ] 删除所有 try/except 块
- [ ] 重构 `stop_stream()` 方法
  - [ ] 提取业务逻辑到 `_stop_stream_impl()`
  - [ ] 业务代码只抛异常
- [ ] 添加 `@log_call` 装饰器
- [ ] 异常分类正确
  - [ ] StreamConnectionError: RTSP/RTMP 连接失败
  - [ ] FFmpegError: FFmpeg 启动失败

### 5.2 decoder.py

- [ ] 重构 `start()` 方法
  - [ ] 提取业务逻辑到 `_launch_ffmpeg_process()`
  - [ ] 只抛 FFmpegError
  - [ ] 删除所有 try/except 块
- [ ] 重构 `run()` 方法
  - [ ] 添加边界层 1 异常捕获
  - [ ] 业务逻辑 `_process_frames()` 保持纯净
- [ ] 添加 `@timing` 装饰器监控性能
- [ ] 异常分类正确
  - [ ] FFmpegError: 所有 FFmpeg 相关错误

### 5.3 health_monitor.py

- [ ] 在 `__init__` 中创建 RetryExecutor
- [ ] 重构 `_restart_stream()` 方法
  - [ ] 删除手动重试循环
  - [ ] 使用 RetryExecutor
  - [ ] 添加重试回调
- [ ] 重构 `run()` 方法
  - [ ] 添加边界层 1 异常捕获
  - [ ] 业务逻辑保持纯净

### 5.4 cleanup.py

- [ ] 清理方法纯净化
  - [ ] 删除 try/except 块
  - [ ] 失败时抛出 PersistenceError

---

## 六、测试计划

### 6.1 单元测试

创建 `tests/services/test_stream_service_boundary.py`：

- [ ] 测试 `start_stream()` 成功
- [ ] 测试 `start_stream()` 重试后成功
- [ ] 测试 `start_stream()` 达到最大重试次数
- [ ] 测试 `start_stream()` 不可重试异常
- [ ] 测试 `stop_stream()` 成功
- [ ] 测试 FFmpegDecoder 线程异常捕获

### 6.2 集成测试

- [ ] 测试完整流程：启动流 → 健康监控 → 流断线 → 自动重启
- [ ] 测试 API 异常处理：调用 API → 流启动失败 → 返回 503 状态码

---

## 七、回滚计划

如果迁移出现问题，可以按以下步骤回滚：

1. 使用 Git 回滚到迁移前的提交
2. 保留新增的异常类和 RetryExecutor 代码（可能在其他地方使用）
3. 恢复旧的 try/except 异常处理逻辑

---

## 八、预期收益

### 8.1 代码质量

- ✅ 业务代码纯净，语义清晰
- ✅ 删除 37 处泛化异常处理
- ✅ 异常分类准确，便于调试

### 8.2 可维护性

- ✅ 重试逻辑统一管理，易于调整
- ✅ 边界层职责明确，易于理解
- ✅ 测试覆盖完整，易于验证

### 8.3 健壮性

- ✅ Worker 线程不会因异常崩溃
- ✅ 自动重试减少瞬态失败
- ✅ 全局异常处理器统一错误响应

---

## 九、时间估算

| 阶段 | 任务 | 预计时间 |
|------|------|---------|
| 阶段 2 | service.py 迁移 | 2-3 小时 |
| 阶段 3 | decoder.py 迁移 | 1-2 小时 |
| 阶段 4 | health_monitor.py 迁移 | 1 小时 |
| 阶段 5 | cleanup.py 迁移 | 0.5 小时 |
| 测试 | 单元测试 + 集成测试 | 1-2 小时 |
| 总计 | | **5-8 小时** |

---

## 十、参考文档

- [边界层异常处理架构](C:\Users\31399\.claude\plans\boundary-layer-exception-handling.md)
- [边界层异常处理示例](app/utils/BOUNDARY_LAYER_EXAMPLES.md)
- [RetryExecutor 文档](app/utils/executor.py)
- [CleanSight 异常类文档](app/utils/exceptions.py)
