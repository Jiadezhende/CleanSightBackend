"""`clean_task` 表：ORM 映射（DB schema 单一真源）+ 只读查询。

    from app.db import tasks as db_tasks
    row = db_tasks.query_task(42)                         # Optional[DBTask]
    ips = db_tasks.query_source_ips([1, 2])               # {task_id: source_ip}
    total, rows = db_tasks.query_task_page("10.0", limit=50, offset=0)

- 每个 `query_*` 自开自关一个 session；返回的是**会话已关闭**的 ORM 实例（无 relationship、
  不 commit，列属性照常可读）——别对它们做写操作。
- 失败一律抛 `app.types.exceptions.DatabaseError`（边界层转 503）；降级策略归调用方。
"""

from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import BigInteger, Column, String, Text, or_
from sqlalchemy.exc import SQLAlchemyError

from app.types.exceptions import DatabaseError

from .database import Base, SessionLocal


# NOTE: 无代码平台托管表，_id 是平台主键(varchar)，业务主键是 task_id
class DBTask(Base):
    __tablename__ = "clean_task"

    _id = Column(String, primary_key=True)  # 平台主键 (varchar)
    cls_id = Column(String, nullable=False)  # 平台 class 标识 (NOT NULL)
    task_id = Column(BigInteger, nullable=False, index=True)  # 业务主键
    source_ip = Column(Text, index=True)
    current_step = Column(Text, default="0")
    status = Column(Text, default="paused")
    updated_time = Column(BigInteger)
    start_time = Column(BigInteger, default=0)
    end_time = Column(BigInteger, default=0)


def query_task(task_id: int) -> Optional[DBTask]:
    """按业务主键取一行；不存在返回 None。

    Raises:
        DatabaseError: "Failed to query task {task_id}"
    """
    try:
        with SessionLocal() as s:
            return s.query(DBTask).filter(DBTask.task_id == task_id).first()
    except SQLAlchemyError as e:
        raise DatabaseError(
            message=f"Failed to query task {task_id}",
            retryable=True,
            query="task_lookup_by_id",
        ) from e


def query_source_ips(task_ids: Sequence[int]) -> Dict[int, Optional[str]]:
    """批量取 task_id → source_ip（单次 IN 查询）；DB 里没有的 task_id 不出现在结果里。

    空输入直接返回 {}，不开 session。

    Raises:
        DatabaseError: "Failed to query source_ip for tasks"
    """
    if not task_ids:
        return {}
    try:
        with SessionLocal() as s:
            rows = (
                s.query(DBTask.task_id, DBTask.source_ip)
                .filter(DBTask.task_id.in_(list(task_ids)))
                .all()
            )
            return {int(r.task_id): r.source_ip for r in rows}
    except SQLAlchemyError as e:
        raise DatabaseError(
            message="Failed to query source_ip for tasks",
            retryable=True,
            query="SELECT task_id, source_ip FROM clean_task WHERE task_id IN (...)",
        ) from e


def query_task_page(
    needle: Optional[str], *, limit: int, offset: int
) -> Tuple[int, List[DBTask]]:
    """按关键字筛 → (命中总数, 本页行)，updated_time、task_id 双降序。

    `needle` 先 strip，空则不筛；非空时 source_ip / status 大小写不敏感子串匹配，能转整数时
    再 OR 上 task_id 相等。count 与翻页在同一 session 内。

    Raises:
        DatabaseError: "Failed to list lab tasks"
    """
    try:
        with SessionLocal() as s:
            query = s.query(DBTask)
            needle = (needle or "").strip()
            if needle:
                filters = [
                    DBTask.source_ip.ilike(f"%{needle}%"),
                    DBTask.status.ilike(f"%{needle}%"),
                ]
                task_id = _as_int(needle)
                if task_id is not None:
                    filters.append(DBTask.task_id == task_id)
                query = query.filter(or_(*filters))

            total = query.count()
            rows = (
                query
                .order_by(DBTask.updated_time.desc(), DBTask.task_id.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return int(total), rows
    except SQLAlchemyError as e:
        raise DatabaseError(
            message="Failed to list lab tasks",
            retryable=True,
            query="SELECT ... FROM clean_task",
        ) from e


def _as_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
