"""
Lab 视频段导出与 Label Studio 提交。

模块：
- clip_builder: 按 [start_ms, end_ms] 区间从 raw 段拼接出单个 ms 精度 mp4
- label_studio_client: Label Studio HTTP API 极简客户端（multipart 上传 mp4）

被路由层 app/routers/lab.py 使用。
"""

from .clip_builder import (
    ClipBuildError,
    ClipBuilder,
    ClipRangeGapError,
    ClipRangeOutOfBoundsError,
    ClipResult,
    ClipSpec,
)
from .label_studio_client import (
    LabelStudioClient,
    LabelStudioError,
    LabelStudioTaskResult,
)

__all__ = [
    "ClipBuilder",
    "ClipSpec",
    "ClipResult",
    "ClipBuildError",
    "ClipRangeOutOfBoundsError",
    "ClipRangeGapError",
    "LabelStudioClient",
    "LabelStudioError",
    "LabelStudioTaskResult",
]
