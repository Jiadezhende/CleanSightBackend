"""
追溯服务（traceback）

提供任务/告警的视频片段定位与媒体 token 签发。

模块：
- segment_finder: 文件名 ts_us 二分查找定位 HLS 段
  落盘约定：{base_dir}/{task_id}/{step_id}/{raw|processed}_segment_{ts_us}.mp4
- media_token: HMAC 短 TTL 签名 token，用于 /media/* 鉴权（payload 含 task_id/step_id）

注：旧版 locator.resolve_client_id（查 clean_task.source_ip）已废弃 —— 该字段在
step 切洗消台时会被业务侧覆写，无法作为可靠的 task_id → 文件位置映射。
"""

from .media_token import MediaToken, MediaTokenError
from .segment_finder import SegmentFinder, SegmentRef

__all__ = [
    "MediaToken",
    "MediaTokenError",
    "SegmentFinder",
    "SegmentRef",
]
