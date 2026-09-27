"""
追溯服务（traceback）

对外只剩**媒体 token 的签发与校验**：

- media_token: HMAC 短 TTL 签名 token，用于 /media/* 鉴权（payload 含 task_id/step_id/可选 run_id）

段的定位与枚举在数据层 `app.storage.hls`（落盘 `{root}/{task}/{step}/{run_id}/hls/`）。

注：旧版 locator.resolve_client_id（查 clean_task.source_ip）已废弃 —— 该字段在
step 切洗消台时会被业务侧覆写，无法作为可靠的 task_id → 文件位置映射。
"""

from .media_token import MediaToken, MediaTokenError, MediaTokenPayload

__all__ = [
    "MediaToken",
    "MediaTokenError",
    "MediaTokenPayload",
]
