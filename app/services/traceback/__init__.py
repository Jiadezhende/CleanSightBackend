"""
追溯服务（traceback）

提供任务/告警的视频片段定位、媒体 token 签发与 client_id 解析。

模块：
- locator: task_id → client_id 解析（查 clean_task.source_ip）
- segment_finder: 文件名 ts_us 二分查找定位 HLS 段与 keypoints JSON
- media_token: HMAC 短 TTL 签名 token，用于 /media/* 鉴权
"""

from .locator import resolve_client_id
from .media_token import MediaToken, MediaTokenError
from .segment_finder import SegmentFinder, SegmentRef

__all__ = [
    "resolve_client_id",
    "MediaToken",
    "MediaTokenError",
    "SegmentFinder",
    "SegmentRef",
]
