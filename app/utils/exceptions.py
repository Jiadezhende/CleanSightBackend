"""
CleanSight 自定义异常层次结构

简化设计，专注实用性：
- 5个核心异常类覆盖主要场景
- 所有异常携带 client_id 和 retry_able 标识
- 避免过度设计，适用于并发 <15 的小规模系统
"""


class CleanSightException(Exception):
    """CleanSight 基础异常类

    所有自定义异常的基类，提供：
    - client_id: 客户端标识（用于日志关联）
    - retry_able: 是否可重试标识
    """

    def __init__(self, message: str, client_id: str = None, retry_able: bool = False):
        """初始化异常

        Args:
            message: 错误消息
            client_id: 客户端ID（可选）
            retry_able: 是否可重试（默认False）
        """
        super().__init__(message)
        self.message = message
        self.client_id = client_id
        self.retry_able = retry_able

    def __str__(self):
        """字符串表示"""
        parts = [self.message]
        if self.client_id:
            parts.append(f"[client_id={self.client_id}]")
        if self.retry_able:
            parts.append("[retry_able]")
        return " ".join(parts)


# ============================================================================
# 服务级别异常（5个核心异常）
# ============================================================================

class StreamConnectionError(CleanSightException):
    """RTSP/RTMP 流连接失败

    用于：
    - RTSP/RTMP 连接超时
    - 流媒体服务器不可达
    - 网络连接问题

    特点：默认可重试（retry_able=True）
    """

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
        super().__init__(message, client_id=client_id, retry_able=True)
        self.url = url
        self.details = details


class FFmpegError(CleanSightException):
    """FFmpeg 解码错误

    用于：
    - FFmpeg 进程启动失败
    - FFmpeg 二进制文件未找到
    - 视频解码错误
    - FFmpeg 进程异常退出

    特点：通常不可重试（retry_able=False）
    """

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
        super().__init__(full_message, client_id=client_id, retry_able=False)
        self.exit_code = exit_code
        self.stderr = stderr


class DatabaseError(CleanSightException):
    """数据库错误

    用于：
    - 数据库连接失败
    - SQL 查询错误
    - 事务提交失败
    - 连接池耗尽

    特点：默认可重试（retry_able=True）
    """

    def __init__(self, message: str, client_id: str = None, retry_able: bool = True, query: str = None):
        """初始化数据库错误

        Args:
            message: 错误消息
            client_id: 客户端ID
            retry_able: 是否可重试
            query: 失败的SQL查询（可选）
        """
        super().__init__(f"Database error: {message}", client_id=client_id, retry_able=retry_able)
        self.query = query


class ModelInferenceError(CleanSightException):
    """模型推理错误

    用于：
    - 模型加载失败
    - CUDA 内存不足
    - 推理超时
    - 批量推理失败

    特点：通常不可重试（retry_able=False），但 CUDA OOM 可以标记为可重试
    """

    def __init__(self, message: str, client_id: str = None, model_name: str = None,
                 retry_able: bool = False, is_cuda_error: bool = False):
        """初始化模型推理错误

        Args:
            message: 错误消息
            client_id: 客户端ID
            model_name: 模型名称
            retry_able: 是否可重试
            is_cuda_error: 是否为CUDA错误
        """
        full_message = f"Model inference error: {message}"
        if model_name:
            full_message += f" (model={model_name})"
        super().__init__(full_message, client_id=client_id, retry_able=retry_able)
        self.model_name = model_name
        self.is_cuda_error = is_cuda_error


class PersistenceError(CleanSightException):
    """持久化错误（HLS 视频段写入、告警上报）

    用于：
    - HLS 视频段写入失败
    - M3U8 播放列表生成失败
    - 告警上报到外部 API 失败
    - 文件系统错误

    特点：默认可重试（retry_able=True）
    """

    def __init__(self, message: str, client_id: str = None, operation: str = None,
                 retry_able: bool = True):
        """初始化持久化错误

        Args:
            message: 错误消息
            client_id: 客户端ID
            operation: 操作类型（如 "hls_write", "alarm_report"）
            retry_able: 是否可重试
        """
        full_message = f"Persistence error: {message}"
        if operation:
            full_message += f" (operation={operation})"
        super().__init__(full_message, client_id=client_id, retry_able=retry_able)
        self.operation = operation


# ============================================================================
# 工具函数
# ============================================================================

def is_retryable_error(exception: Exception) -> bool:
    """检查异常是否可重试

    Args:
        exception: 异常对象

    Returns:
        bool: 如果是 CleanSightException 且 retry_able=True，则返回 True
    """
    if isinstance(exception, CleanSightException):
        return exception.retry_able
    return False


def get_client_id_from_exception(exception: Exception) -> str:
    """从异常中提取 client_id

    Args:
        exception: 异常对象

    Returns:
        str: client_id，如果不存在则返回 None
    """
    if isinstance(exception, CleanSightException):
        return exception.client_id
    return None
