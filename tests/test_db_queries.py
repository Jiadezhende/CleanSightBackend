"""`app.db.tasks` / `app.db.alarms` 的只读查询。

不连真实 DB：`SessionLocal` 换成绑在内存 SQLite 上的 sessionmaker，查询条件 / 排序 / 分页
按真实 SQL 执行（SQLite 把 ilike 编成 lower() LIKE lower()，语义同 PostgreSQL 的 ILIKE）。
失败路径用「表没建」的引擎触发真实 OperationalError。
"""

from typing import List

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import alarms as db_alarms
from app.db import tasks as db_tasks
from app.db.alarms import DBAlarm
from app.db.database import Base
from app.db.tasks import DBTask
from app.types.exceptions import DatabaseError, ValidationError


class _TrackingSession(Session):
    """记录 close() 是否被调用；每个实例登记到类级列表里。"""

    opened: List["_TrackingSession"] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.was_closed = False
        _TrackingSession.opened.append(self)

    def close(self):
        self.was_closed = True
        super().close()


def _factory(create_tables: bool):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    if create_tables:
        Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=_TrackingSession)


def _install(monkeypatch, factory):
    _TrackingSession.opened = []
    monkeypatch.setattr(db_tasks, "SessionLocal", factory)
    monkeypatch.setattr(db_alarms, "SessionLocal", factory)


@pytest.fixture
def db(monkeypatch):
    """装好空表的 sessionmaker；用例往里 add 行后 commit。"""
    factory = _factory(create_tables=True)
    _install(monkeypatch, factory)
    return factory


@pytest.fixture
def broken_db(monkeypatch):
    """表不存在 → 任何查询都抛 OperationalError（SQLAlchemyError 子类）。"""
    _install(monkeypatch, _factory(create_tables=False))


def _seed(factory, *rows):
    with factory() as s:
        s.add_all(rows)
        s.commit()
    _TrackingSession.opened = []    # 只统计被测函数开的 session


def _task(task_id, *, source_ip="10.0.0.1", status="running", updated_time=0, **kw):
    return DBTask(
        _id=f"t{task_id}", cls_id="c", task_id=task_id, source_ip=source_ip,
        status=status, updated_time=updated_time, **kw,
    )


def _alarm(alarm_id, task_id, *, step_id=1, create_time=0, detected_at=0):
    return DBAlarm(
        _id=f"a{alarm_id}", alarm_id=alarm_id, task_id=task_id, step_id=step_id,
        create_time=create_time, detected_at=detected_at,
    )


def _assert_all_closed(expected_count=1):
    assert len(_TrackingSession.opened) == expected_count
    assert all(s.was_closed for s in _TrackingSession.opened)


# ---------------------------------------------------------------------------
# query_task
# ---------------------------------------------------------------------------


class TestQueryTask:
    def test_returns_detached_row_with_readable_columns(self, db):
        _seed(db, _task(42, source_ip="1.2.3.4", current_step="3"), _task(43))

        row = db_tasks.query_task(42)

        assert row.task_id == 42
        assert row.source_ip == "1.2.3.4"
        assert row.current_step == "3"
        assert inspect(row).detached
        _assert_all_closed()

    def test_missing_returns_none(self, db):
        _seed(db, _task(1))
        assert db_tasks.query_task(999) is None
        _assert_all_closed()

    def test_sqlalchemy_error_becomes_database_error(self, broken_db):
        with pytest.raises(DatabaseError) as ei:
            db_tasks.query_task(42)

        # str(exc) 就是 503 body 的 detail，须与旧 router 逐字一致
        assert str(ei.value) == "Database error: Failed to query task 42 [retryable]"
        assert ei.value.query == "task_lookup_by_id"
        _assert_all_closed()


# ---------------------------------------------------------------------------
# query_source_ips
# ---------------------------------------------------------------------------


class TestQuerySourceIps:
    def test_maps_found_ids_and_skips_missing(self, db):
        _seed(db, _task(1, source_ip="a"), _task(2, source_ip=None), _task(3, source_ip="c"))

        assert db_tasks.query_source_ips((1, 2, 404)) == {1: "a", 2: None}
        _assert_all_closed()

    def test_empty_input_does_not_open_session(self, db):
        assert db_tasks.query_source_ips([]) == {}
        assert _TrackingSession.opened == []

    def test_sqlalchemy_error_becomes_database_error(self, broken_db):
        with pytest.raises(DatabaseError) as ei:
            db_tasks.query_source_ips([1])
        assert ei.value.message == "Database error: Failed to query source_ip for tasks"
        _assert_all_closed()


# ---------------------------------------------------------------------------
# query_tasks
# ---------------------------------------------------------------------------


class TestQueryTasks:
    def test_maps_found_ids_to_rows_and_skips_missing(self, db):
        _seed(db, _task(1, source_ip="a", status="running"), _task(2, source_ip=None), _task(3))

        rows = db_tasks.query_tasks((1, 2, 404))
        assert sorted(rows) == [1, 2]
        assert (rows[1].source_ip, rows[1].status) == ("a", "running")
        assert rows[2].source_ip is None
        _assert_all_closed()

    def test_empty_input_does_not_open_session(self, db):
        assert db_tasks.query_tasks([]) == {}
        assert _TrackingSession.opened == []

    def test_sqlalchemy_error_becomes_database_error(self, broken_db):
        with pytest.raises(DatabaseError) as ei:
            db_tasks.query_tasks([1])
        assert ei.value.message == "Database error: Failed to query tasks"
        _assert_all_closed()


# ---------------------------------------------------------------------------
# query_task_alarms / query_step_alarms
# ---------------------------------------------------------------------------


class TestAlarmQueries:
    @pytest.fixture
    def seeded(self, db):
        _seed(
            db,
            _alarm(1, 42, step_id=1, create_time=10, detected_at=300),
            _alarm(2, 42, step_id=2, create_time=30, detected_at=100),
            _alarm(3, 42, step_id=1, create_time=20, detected_at=200),
            _alarm(4, 43, step_id=1, create_time=40, detected_at=50),
        )
        return db

    def test_task_alarms_create_time_desc(self, seeded):
        rows = db_alarms.query_task_alarms(42)
        assert [r.alarm_id for r in rows] == [2, 3, 1]
        assert all(inspect(r).detached for r in rows)
        _assert_all_closed()

    def test_step_alarms_filtered_and_detected_at_asc(self, seeded):
        rows = db_alarms.query_step_alarms(42, 1)
        assert [r.alarm_id for r in rows] == [3, 1]
        _assert_all_closed()

    def test_empty(self, seeded):
        assert db_alarms.query_task_alarms(999) == []
        assert db_alarms.query_step_alarms(42, 999) == []

    def test_task_alarms_error(self, broken_db):
        with pytest.raises(DatabaseError) as ei:
            db_alarms.query_task_alarms(42)
        assert str(ei.value) == "Database error: Failed to fetch alarms for task 42 [retryable]"
        assert ei.value.query == "SELECT ... FROM clean_alarm WHERE task_id = 42"
        _assert_all_closed()

    def test_step_alarms_error(self, broken_db):
        with pytest.raises(DatabaseError) as ei:
            db_alarms.query_step_alarms(42, 1)
        assert str(ei.value) == "Database error: Failed to fetch alarms for task 42 [retryable]"
        assert ei.value.query is None
        _assert_all_closed()


# ---------------------------------------------------------------------------
# detected_at_ms
# ---------------------------------------------------------------------------


class TestDetectedAtMs:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (1_700_000_000, 1_700_000_000_000),            # 秒
            (10**11 - 1, (10**11 - 1) * 1000),             # 秒上界
            (10**11, 10**11),                              # 毫秒下界
            (1_700_000_000_123, 1_700_000_000_123),        # 毫秒
            (10**14 - 1, 10**14 - 1),                      # 毫秒上界
            (1_700_000_000_123_456, 1_700_000_000_123),    # 微秒
        ],
    )
    def test_normalises_by_magnitude(self, raw, expected):
        assert db_alarms.detected_at_ms(raw) == expected

    def test_none_rejected(self):
        with pytest.raises(ValidationError) as ei:
            db_alarms.detected_at_ms(None)
        assert str(ei.value) == "alarm.detected_at is null"
        assert ei.value.field == "detected_at"

    @pytest.mark.parametrize("raw", [0, -5])
    def test_non_positive_rejected(self, raw):
        with pytest.raises(ValidationError) as ei:
            db_alarms.detected_at_ms(raw)
        assert str(ei.value) == "alarm.detected_at must be positive"
        assert (ei.value.field, ei.value.value) == ("detected_at", str(raw))
