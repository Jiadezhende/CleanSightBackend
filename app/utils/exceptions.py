"""
CleanSight 自定义异常层次结构

基于《实时 AI 视觉检测项目异常处理与边界层设计规范》：
- 异常即协议：异常类型表达语义
- retryable: 是否可重试（瞬时故障）
- fatal: 是否致命（系统级错误，需停止服务）
- 6个核心异常类（AppError + 5个服务异常 + FrameDrop）
"""


class AppError(Exception):
    """应用异常基类

    所有自定义异常的基类，提供：
    - client_id: 客户端标识（用于日志关联）
    - retryable: 是否可重试（默认False）
    - fatal: 是否致命（默认False）

    注意：子类可以通过类属性覆盖 retryable 和 fatal
    """

    retryable: bool = False  # 类属性：默认不可重试
    fatal: bool = False      # 类属性：默认不致命

    def __init__(self, message: str, client_id: str = None, **kwargs):
        """初始化异常

        Args:
            message: 错误消息
            client_id: 客户端ID（可选）
            **kwargs: 可选的 retryable, fatal 覆盖
        """
        super().__init__(message)
        self.message = message
        self.client_id = client_id

        # 允许实例级别覆盖类属性
        if 'retryable' in kwargs:
            self.retryable = kwargs['retryable']
        if 'fatal' in kwargs:
            self.fatal = kwargs['fatal']

    def __str__(self):
        """字符串表示"""
        parts = [self.message]
        if self.client_id:
            parts.append(f"[client_id={self.client_id}]")
        if self.retryable:
            parts.append("[retryable]")
        if self.fatal:
            parts.append("[FATAL]")
        return " ".join(parts)


# ============================================================================
# 帧丢弃异常（实时推理专用）
# ============================================================================

class FrameDrop(AppError):
    """当前帧无效，允许安静丢弃

    用于 30fps 实时推理场景：
    - 单帧解码失败
    - 单帧推理超时
    - 单帧质量检查不通过
    - 客户端已移除但帧仍在队列

    处理策略：
    - 记录 metrics，不打印错误日志
    - Executor 返回 None
    - 继续处理下一帧

    特点：retryable=False, fatal=False
    """

    retryable = False  # 帧已丢失，无需重试
    fatal = False      # 不影响系统运行

    def __init__(self, client_id: str, frame_index: int = None, reason: str = None):
        """初始化帧丢弃异常

        Args:
            client_id: 客户端ID（必填）
            frame_index: 帧索引（可选）
            reason: 丢弃原因（可选，如 "decode_failed", "client_removed"）
        """
        message = f"Frame dropped for {client_id}"
        if frame_index is not None:
            message += f" at index {frame_index}"
        if reason:
            message += f": {reason}"
        super().__init__(message, client_id=client_id)
        self.frame_index = frame_index
        self.reason = reason


# ============================================================================
# 服务级别异常（5个核心异常）
# ============================================================================

class StreamConnectionError(AppError):
    """RTSP/RTMP 流连接失败

    用于：
    - RTSP/RTMP 连接超时
    - 流媒体服务器不可达
    - 网络连接问题

    特点：retryable=True, fatal=False（网络瞬时故障，可重试）
    """

    retryable = True   # 网络瞬时故障，可重试
    fatal = False      # 单路流失败不影响系统

    def __init__(self, url: str, client_id: str = None, details: str = None):
        """初始化流连接错误

        Args:
            url: 流地址
            client_id: 客户端ID
            details: 详细错误信息
        """
        message = f"Stream connection failed: {url}"
        if details:
            message += f" - {details}"
        super().__init__(message, client_id=client_id)
        self.url = url
        self.details = details


class FFmpegError(AppError):
    """FFmpeg 解码错误

    用于：
    - FFmpeg 进程启动失败
    - FFmpeg 二进制文件未找到
    - 视频解码错误
    - FFmpeg 进程异常退出

    特点：retryable=False, fatal=True（解码器崩溃，需要重启流）
    """

    retryable = False  # 编码格式不支持，无法重试
    fatal = True       # 解码器崩溃，需要重启流

    def __init__(self, message: str, client_id: str = None, exit_code: int = None, stderr: str = None):
        """初始化 FFmpeg 错误

        Args:
            message: 错误消息
            client_id: 客户端ID
            exit_code: FFmpeg 进程退出码
            stderr: FFmpeg 标准错误输出
        """
        full_message = f"FFmpeg error: {message}"
        if exit_code is not None:
            full_message += f" (exit_code={exit_code})"
        super().__init__(full_message, client_id=client_id)
        self.exit_code = exit_code
        self.stderr = stderr


class DatabaseError(AppError):
    """数据库错误

    用于：
    - 数据库连接失败
    - SQL 查询错误
    - 事务提交失败
    - 连接池耗尽

    特点：retryable=True, fatal=False（连接池耗尽，可重试）
    """

    retryable = True   # 连接池耗尽，可重试
    fatal = False      # 数据库故障不影响推理主流程

    def __init__(self, message: str, client_id: str = None, retryable: bool = None, query: str = None):
        """初始化数据库错误

        Args:
            message: 错误消息
            client_id: 客户端ID
            retryable: 是否可重试（可选，默认使用类属性）
            query: 失败的SQL查询（可选）
        """
        kwargs = {}
        if retryable is not None:
            kwargs['retryable'] = retryable
        super().__init__(f"Database error: {message}", client_id=client_id, **kwargs)
        self.query = query


class ModelInferenceError(AppError):
    """模型推理错误

    用于：
    - 模型加载失败
    - CUDA 内存不足
    - 推理超时
    - 批量推理失败

    特点：retryable=False, fatal=False（CUDA OOM 等，重试无用；单路失败不影响其他路）
    """

    retryable = False  # CUDA OOM 等，重试无用
    fatal = False      # 单路失败不影响其他路

    def __init__(self, message: str, client_id: str = None, model_name: str = None,
                 retryable: bool = None, is_cuda_error: bool = False):
        """初始化模型推理错误

        Args:
            message: 错误消息
            client_id: 客户端ID
            model_name: 模型名称
            retryable: 是否可重试（可选，默认使用类属性）
            is_cuda_error: 是否为CUDA错误
        """
        full_message = f"Model inference error: {message}"
        if model_name:
            full_message += f" (model={model_name})"
        kwargs = {}
        if retryable is not None:
            kwargs['retryable'] = retryable
        super().__init__(full_message, client_id=client_id, **kwargs)
        self.model_name = model_name
        self.is_cuda_error = is_cuda_error


class PersistenceError(AppError):
    """持久化错误（HLS 视频段写入、告警上报）

    用于：
    - HLS 视频段写入失败
    - M3U8 播放列表生成失败
    - 告警上报到外部 API 失败
    - 文件系统错误

    特点：retryable=True, fatal=False（磁盘临时满，可重试）
    """

    retryable = True   # 磁盘临时满，可重试
    fatal = False      # 持久化失败不影响推理主流程

    def __init__(self, message: str, client_id: str = None, operation: str = None,
                 retryable: bool = None):
        """初始化持久化错误

        Args:
            message: 错误消息
            client_id: 客户端ID
            operation: 操作类型（如 "hls_write", "alarm_report"）
            retryable: 是否可重试（可选，默认使用类属性）
        """
        full_message = f"Persistence error: {message}"
        if operation:
            full_message += f" (operation={operation})"
        kwargs = {}
        if retryable is not None:
            kwargs['retryable'] = retryable
        super().__init__(full_message, client_id=client_id, **kwargs)
        self.operation = operation


# ============================================================================
# 工具函数
# ============================================================================

def is_retryable_error(exception: Exception) -> bool:
    """检查异常是否可重试

    Args:
        exception: 异常对象

    Returns:
        bool: 如果是 AppError 且 retryable=True，则返回 True
    """
    if isinstance(exception, AppError):
        return exception.retryable
    return False


def is_fatal_error(exception: Exception) -> bool:
    """检查异常是否致命

    Args:
        exception: 异常对象

    Returns:
        bool: 如果是 AppError 且 fatal=True，则返回 True
    """
    if isinstance(exception, AppError):
        return exception.fatal
    return False


def get_client_id_from_exception(exception: Exception) -> str:
    """从异常中提取 client_id

    Args:
        exception: 异常对象

    Returns:
        str: client_id，如果不存在则返回 None
    """
    if isinstance(exception, AppError):
        return exception.client_id
    return None
