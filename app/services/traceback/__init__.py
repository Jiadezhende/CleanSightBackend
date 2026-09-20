"""
追溯服务（traceback）

对外只剩**媒体 token 的签发与校验**：

- media_token: HMAC 短 TTL 签名 token，用于 /media/* 鉴权（payload 含 task_id/step_id）

段的定位与枚举已下沉到数据层 `app.storage.hls`（落盘 `{root}/{task}/{step}/hls/`）。
本包里的 `segment_finder` 是**没有调用点的旧代码**（读的是旧平铺布局），故刻意不在此
re-export：留着 `SegmentFinder` / `SegmentRef` / `StepRef` 会让残留引用静默拿到旧型
（旧 `SegmentRef` 6 字段、新的 2 字段，混用不会立刻报错），而不是当场 `ImportError`。

注：旧版 locator.resolve_client_id（查 clean_task.source_ip）已废弃 —— 该字段在
step 切洗消台时会被业务侧覆写，无法作为可靠的 task_id → 文件位置映射。
"""

from .media_token import MediaToken, MediaTokenError

__all__ = [
    "MediaToken",
    "MediaTokenError",
]
