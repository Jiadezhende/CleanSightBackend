"""SessionService：注册约束、身份核对的 close、按点位解析。"""

import pytest

from app.services.session.core import SessionInfo, SessionState
from app.services.session.service import SessionConflictError, SessionService
from app.types.run import RunIdentity


def _info(task_id=1, ip="ip", started_at=1.0, run_id=1):
    return SessionInfo(run=RunIdentity(task_id, 1, run_id), source_ip=ip, stage="1", started_at=started_at)


def test_open_registers_and_rejects_second():
    svc = SessionService()
    sess = svc.open(_info())
    assert svc.get(1) is sess and svc.has(1)
    with pytest.raises(SessionConflictError):
        svc.open(_info(run_id=2))
    assert svc.get(1) is sess


def test_close_removes_only_matching_and_always_closes():
    svc = SessionService()
    old = svc.open(_info())
    assert svc.close(old) is True
    assert not svc.has(1)
    assert old.state is SessionState.CLOSED

    new = svc.open(_info(run_id=2))
    assert svc.close(old) is False          # 迟到的旧 close 不摘新 run
    assert svc.get(1) is new and new.is_active()


def test_seal_keeps_registration():
    svc = SessionService()
    sess = svc.open(_info())
    assert svc.seal(sess) is True
    assert svc.get(1) is sess and sess.state is SessionState.DRAINING


def test_snapshot_is_readonly_and_stable():
    svc = SessionService()
    svc.open(_info(1))
    snap = svc.snapshot()
    svc.open(_info(2))
    assert list(snap) == [1]
    with pytest.raises(TypeError):
        snap[3] = None


def test_find_by_source_ip_picks_latest_started():
    svc = SessionService()
    svc.open(_info(1, ip="a", started_at=1.0))
    later = svc.open(_info(2, ip="a", started_at=2.0))
    svc.open(_info(3, ip="b", started_at=3.0))
    assert svc.find_by_source_ip("a") is later
    assert svc.find_by_source_ip("zzz") is None
