"""
task_id → client_id 解析

直接查 `clean_task.source_ip`（已有 indexed 字段就是 client_id），用 LRU 缓存
减少重复查询。一旦写入，task_id → source_ip 不会变，缓存永不失效。
"""

import logging
from functools import lru_cache
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models.task import DBTask
from app.utils.exceptions import DatabaseError

logger = logging.getLogger(__name__)


_CACHE_MAXSIZE = 1024


@lru_cache(maxsize=_CACHE_MAXSIZE)
def _query_source_ip(task_id: int) -> Optional[str]:
    """从数据库查询 task_id 对应的 source_ip。

    Raises:
        DatabaseError: 数据库查询失败（可重试）
    """
    db = SessionLocal()
    try:
        try:
            row = (
                db.query(DBTask.source_ip)
                .filter(DBTask.task_id == int(task_id))
                .first()
            )
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to resolve client_id for task_id={task_id}",
                retryable=True,
                query=f"SELECT source_ip FROM clean_task WHERE task_id = {task_id}",
            ) from e

        if row is None or row[0] is None:
            return None
        return str(row[0])
    finally:
        db.close()


def resolve_client_id(task_id: int) -> Optional[str]:
    """解析 task_id 对应的 client_id（即 clean_task.source_ip）。

    返回 None 表示任务不存在或未关联 source_ip，由上层 API 决定 404 处理。

    Raises:
        DatabaseError: 数据库查询失败（可重试）
    """
    if task_id is None:
        return None
    try:
        return _query_source_ip(int(task_id))
    except (TypeError, ValueError):
        # task_id 不是合法整数，返回 None 让上层处理
        logger.warning("resolve_client_id: invalid task_id=%r", task_id)
        return None


def invalidate_cache(task_id: Optional[int] = None) -> None:
    """清除缓存。task_id=None 时清除全部。

    主要用于测试；正常运行不需要调用，因为 task_id 与 source_ip 是一一不变的关系。
    """
    if task_id is None:
        _query_source_ip.cache_clear()
    else:
        # functools.lru_cache 没有 per-key 失效；退化为全清
        _query_source_ip.cache_clear()
