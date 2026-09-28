"""`clean_alarm` 表：ORM 映射（DB schema 单一真源）+ 只读查询。

    from app.db import alarms as db_alarms
    rows = db_alarms.query_task_alarms(42)        # create_time 降序
    rows = db_alarms.query_step_alarms(42, 3)     # detected_at 升序
    ms = db_alarms.detected_at_ms(row.detected_at)

- 每个 `query_*` 自开自关一个 session；返回的是**会话已关闭**的 ORM 实例，别对它们做写操作。
- 失败一律抛 `app.types.exceptions.DatabaseError`（边界层转 503）。
- `detected_at` 的单位由平台写入方决定（秒 / 毫秒 / 微秒都见过），按位数归一，只能走 `detected_at_ms`。
"""

from typing import List, Optional, Sequence

from sqlalchemy import BigInteger, Boolean, Column, String, Text
from sqlalchemy.exc import SQLAlchemyError

from app.types.exceptions import DatabaseError, ValidationError

from .database import Base, SessionLocal


class DBAlarm(Base):
    """告警表 ORM（只映射业务字段，忽略平台 hidden 字段）。"""

    __tablename__ = "clean_alarm"

    _id = Column(String, primary_key=True)  # 平台主键 (varchar)
    alarm_id = Column(BigInteger, nullable=False, index=True)  # 业务主键
    task_id = Column(BigInteger, nullable=False, index=True)
    step_id = Column(BigInteger)
    step_name = Column(Text)
    alarm_type = Column(Text)
    severity = Column(Text)  # HTTP 上报时叫 alarm_level
    message = Column(Text)  # HTTP 上报时叫 alarm_message
    detected_at = Column(BigInteger)  # HTTP 上报时叫 alarm_time
    resolved = Column(Boolean, default=False)
    resolved_by = Column(BigInteger)
    resolved_at = Column(BigInteger)
    create_time = Column(BigInteger)  # 平台创建时间，用于排序


def query_task_alarms(task_id: int) -> List[DBAlarm]:
    """该 task 的全部告警，create_time 降序。

    Raises:
        DatabaseError: "Failed to fetch alarms for task {task_id}"
    """
    return _query(
        [DBAlarm.task_id == int(task_id)],
        DBAlarm.create_time.desc(),
        task_id=task_id,
        sql=f"SELECT ... FROM clean_alarm WHERE task_id = {task_id}",
    )


def query_step_alarms(task_id: int, step_id: int) -> List[DBAlarm]:
    """该 task 某个 step 的告警，detected_at 升序。

    Raises:
        DatabaseError: "Failed to fetch alarms for task {task_id}"
    """
    return _query(
        [DBAlarm.task_id == int(task_id), DBAlarm.step_id == int(step_id)],
        DBAlarm.detected_at.asc(),
        task_id=task_id,
    )


def detected_at_ms(detected_at: Optional[int]) -> int:
    """把 detected_at 归一到毫秒：< 1e11 视为秒、< 1e14 视为毫秒、其余视为微秒。

    Raises:
        ValidationError: None → "alarm.detected_at is null"；≤ 0 → "alarm.detected_at must be positive"
    """
    if detected_at is None:
        raise ValidationError("alarm.detected_at is null", field="detected_at")
    v = int(detected_at)
    if v <= 0:
        raise ValidationError(
            "alarm.detected_at must be positive", field="detected_at", value=str(v)
        )
    if v < 10**11:        # 秒级
        return v * 1000
    if v < 10**14:        # 毫秒级
        return v
    return v // 1000      # 微秒级或更高


def _query(filters: Sequence, order_by, *, task_id: int, sql: Optional[str] = None) -> List[DBAlarm]:
    # task_id / sql 只用于构造错误：不传 DatabaseError(task_id=...)，否则 str(exc) 会多出
    # "[task=...]"，改掉 503 body 的 detail
    try:
        with SessionLocal() as s:
            return s.query(DBAlarm).filter(*filters).order_by(order_by).all()
    except SQLAlchemyError as e:
        raise DatabaseError(
            message=f"Failed to fetch alarms for task {task_id}",
            retryable=True,
            query=sql,
        ) from e
