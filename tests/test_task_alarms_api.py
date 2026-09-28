"""GET /task/{task_id}/alarms：DB 行 → DTO 映射、顺序透传、DB 失败 503。

DB 替换 `db_alarms.query_task_alarms`（排序是它的契约，见 tests/test_db_queries.py）；
503 用例走真实查询、让 `SessionLocal` 抛 `SQLAlchemyError`，断言 detail 原文。
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from app.db import alarms as db_alarms
from app.main import app


def _row(**overrides):
    row = dict(
        alarm_id=1, task_id=42, step_id=3, step_name="s3", alarm_type="BUBBLE",
        severity="high", message="m", resolved=None, resolved_by=None,
        detected_at=None, resolved_at=None,
    )
    row.update(overrides)
    return SimpleNamespace(**row)


async def _get(path):
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_rows_mapped_in_query_order(monkeypatch):
    rows = [
        _row(alarm_id=2, resolved=True, resolved_by=7, detected_at=1_700_000_000_000,
             resolved_at=1_700_000_001_000),
        _row(alarm_id=1),  # resolved=None → False；时间字段 None 原样
    ]
    seen = []
    monkeypatch.setattr(
        db_alarms, "query_task_alarms", lambda task_id: seen.append(task_id) or rows
    )

    resp = await _get("/task/42/alarms")

    assert resp.status_code == 200
    assert seen == [42]
    assert resp.json() == {
        "task_id": 42,
        "total": 2,
        "alarms": [
            {
                "alarm_id": 2, "task_id": 42, "step_id": 3, "step_name": "s3",
                "alarm_type": "BUBBLE", "severity": "high", "message": "m",
                "resolved": True, "resolved_by": 7,
                "detected_at": 1_700_000_000_000, "resolved_at": 1_700_000_001_000,
            },
            {
                "alarm_id": 1, "task_id": 42, "step_id": 3, "step_name": "s3",
                "alarm_type": "BUBBLE", "severity": "high", "message": "m",
                "resolved": False, "resolved_by": None,
                "detected_at": None, "resolved_at": None,
            },
        ],
    }


@pytest.mark.asyncio
async def test_empty(monkeypatch):
    monkeypatch.setattr(db_alarms, "query_task_alarms", lambda task_id: [])

    resp = await _get("/task/42/alarms")

    assert resp.json() == {"task_id": 42, "total": 0, "alarms": []}


@pytest.mark.asyncio
async def test_db_failure_is_503(monkeypatch):
    def _boom():
        raise SQLAlchemyError("connection refused")

    monkeypatch.setattr(db_alarms, "SessionLocal", _boom)

    resp = await _get("/task/42/alarms")

    assert resp.status_code == 503
    assert resp.json() == {
        "error": "Database unavailable",
        "detail": "Database error: Failed to fetch alarms for task 42 [retryable]",
        "retryable": True,
    }
