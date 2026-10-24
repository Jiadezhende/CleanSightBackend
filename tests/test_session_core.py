"""session 基建：三种语义、写门、状态机、close 释放、CLOSED 后现造空实例。"""

import threading
from dataclasses import dataclass

import pytest

from app.services.session.core import (
    Fifo, Latest, PressureSpec, Session, SessionInfo, SessionState, TimeWindow, Topic,
)
from app.types.run import RunIdentity


@dataclass
class Item:
    ts: float


def _sess(task_id: int = 1) -> Session:
    return Session(SessionInfo(run=RunIdentity(task_id, 1, 1), source_ip="ip", stage="1"))


LATEST = Topic("LATEST", Latest)
LATEST_LIST = Topic("LATEST_LIST", lambda: Latest(default=[]))
EVICT = Topic("EVICT", lambda: Fifo(maxlen=3, on_full=Fifo.EVICT))
REJECT = Topic("REJECT", lambda: Fifo(maxlen=2, on_full=Fifo.REJECT))
WINDOW = Topic("WINDOW", lambda: TimeWindow(span=10.0))


class TestChannelLifecycle:
    def test_same_topic_same_instance(self):
        s = _sess()
        assert s.channel(LATEST) is s.channel(LATEST)

    def test_sessions_isolated(self):
        a, b = _sess(1), _sess(2)
        a.channel(LATEST).set("x")
        assert b.channel(LATEST).get() is None

    def test_same_name_different_topic(self):
        other = Topic("LATEST", Latest)
        s = _sess()
        s.channel(LATEST).set(1)
        assert s.channel(other).get() is None

    def test_concurrent_first_access_yields_one_instance(self):
        s = _sess()
        got = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            got.append(s.channel(EVICT))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(ch) for ch in got}) == 1


class TestWriteGate:
    def test_draining_rejects_writes_allows_reads_and_clear(self):
        s = _sess()
        s.channel(LATEST).set("a")
        s.channel(EVICT).push(Item(1))
        assert s.seal() is True
        assert s.state is SessionState.DRAINING
        assert s.channel(LATEST).set("b") is False
        assert s.channel(LATEST).get() == "a"
        assert s.channel(EVICT).push(Item(2)) is False
        assert s.channel(WINDOW).push(Item(2)) is False
        assert [i.ts for i in s.channel(EVICT).drain()] == [1]
        s.channel(LATEST).clear()
        assert s.channel(LATEST).get() is None

    def test_seal_idempotent_and_monotonic(self):
        s = _sess()
        assert s.seal() is True
        assert s.seal() is False
        assert s.close() is True
        assert s.seal() is False
        assert s.close() is False
        assert s.state is SessionState.CLOSED

    def test_close_releases_data_held_by_captured_channel(self):
        s = _sess()
        fifo = s.channel(EVICT)
        fifo.push(Item(1))
        latest = s.channel(LATEST_LIST)
        latest.set([1])
        s.close()
        assert len(fifo) == 0
        assert latest.get() == []
        assert fifo.push(Item(2)) is False

    def test_closed_session_returns_detached_empty_instance(self):
        s = _sess()
        s.channel(LATEST).set("a")
        s.close()
        ch = s.channel(LATEST)
        assert ch.get() is None
        assert ch.set("b") is False
        assert s.topics() == []
        assert s.channel(LATEST) is not ch


class TestLatest:
    def test_set_get_clear_default(self):
        s = _sess()
        ch = s.channel(LATEST_LIST)
        assert ch.get() == []
        assert ch.set(["e"]) is True
        assert ch.get() == ["e"]
        ch.clear()
        assert ch.get() == []


class TestFifo:
    def test_evict_drops_oldest_and_counts(self):
        s = _sess()
        q = s.channel(EVICT)
        for t in range(5):
            assert q.push(Item(t)) is True
        assert [i.ts for i in q.drain()] == [2, 3, 4]
        assert q.drops == 2

    def test_reject_refuses_newest_and_counts(self):
        s = _sess()
        q = s.channel(REJECT)
        assert q.push(Item(1)) and q.push(Item(2))
        assert q.push(Item(3)) is False
        assert [i.ts for i in q.drain()] == [1, 2]
        assert q.drops == 1

    def test_pop_take(self):
        s = _sess()
        q = s.channel(EVICT)
        assert q.pop() is None
        for t in range(3):
            q.push(Item(t))
        assert q.take(4) is None
        assert [i.ts for i in q.take(2)] == [0, 1]
        assert q.pop().ts == 2
        assert len(q) == 0

    def test_drain_until_ts_takes_prefix_by_data_time(self):
        s = _sess()
        q = s.channel(EVICT)
        for t in (1, 2, 3):
            q.push(Item(t))
        assert [i.ts for i in q.drain(until_ts=2)] == [1, 2]
        assert [i.ts for i in q.drain(until_ts=2)] == []
        assert len(q) == 1

    def test_drop_reason_increments_metric(self):
        from app.services.utils.metrics import frame_drop_total

        topic = Topic("M", lambda: Fifo(maxlen=1, drop_reason="test_session_drop"))
        before = frame_drop_total.labels(reason="test_session_drop")._value.get()
        q = _sess().channel(topic)
        q.push(Item(1))
        q.push(Item(2))
        assert frame_drop_total.labels(reason="test_session_drop")._value.get() == before + 1

    def test_pressure_reporter_identity_and_reset(self, monkeypatch):
        topic = Topic("P", lambda: Fifo(maxlen=4, pressure=PressureSpec("comp", "res", watermark=2)))
        s = _sess(7)
        q = s.channel(topic)
        rep = q._reporter
        assert (rep.component, rep.resource) == ("comp", "res")
        assert rep._identity == {"task_id": 7, "step_id": 1, "stage": "1"}
        seen = []
        monkeypatch.setattr(rep, "observe", lambda pressured, **kw: seen.append((pressured, kw)))
        q.push(Item(1))
        q.push(Item(2))
        assert [p for p, _ in seen] == [False, True]
        assert seen[-1][1]["depth"] == 2 and seen[-1][1]["capacity"] == 4
        resets = []
        monkeypatch.setattr(rep, "reset", lambda: resets.append(1))
        s.seal()
        assert resets == [1]

    def test_invalid_args(self):
        with pytest.raises(ValueError):
            Fifo(maxlen=0)
        with pytest.raises(ValueError):
            Fifo(maxlen=1, on_full="nope")


class TestTimeWindow:
    def test_evicts_by_data_time(self):
        s = _sess()
        w = s.channel(WINDOW)
        for t in (0, 5, 10, 11):
            w.push(Item(t))
        assert [i.ts for i in w.snapshot()] == [5, 10, 11]   # 0 < 11-10

    def test_extend_span_only_grows(self):
        s = _sess()
        w = s.channel(WINDOW)
        w.extend_span(30)
        w.extend_span(5)
        assert w.span == 30
        for t in (0, 25):
            w.push(Item(t))
        assert len(w) == 2
