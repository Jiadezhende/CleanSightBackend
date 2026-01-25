"""
流服务模块

提供视频流解码和管理的统一接口
"""

from .decoder import FFmpegDecoder
from .service import StreamService, stream_service

__all__ = [
    "FFmpegDecoder",
    "StreamService",
    "stream_service",
]
