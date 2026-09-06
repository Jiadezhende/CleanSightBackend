"""
追溯服务（traceback）

提供 /media/* 的媒体访问鉴权。

模块：
- media_token: HMAC 短 TTL 签名 token，用于 /media/* 鉴权（payload 含 task_id/step_id）

段定位与落盘布局已迁出到 `app.services.step_store`（2026-09）——那是跨服务共享的格式
知识，被 lab / inference.offline / routers 依赖，不该住在"告警回溯取证"这个业务语义包
里。本包只剩鉴权：token 是访问控制，不是落盘格式。

注：旧版 locator.resolve_client_id（查 clean_task.source_ip）已废弃 —— 该字段在
step 切洗消台时会被业务侧覆写，无法作为可靠的 task_id → 文件位置映射。
"""

from .media_token import MediaToken, MediaTokenError

__all__ = [
    "MediaToken",
    "MediaTokenError",
]
