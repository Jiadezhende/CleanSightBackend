"""离线分析父进程侧：spawn 接线与 WorkerProcess 的取结果 / EOF / kill / 回收。不起真进程。"""

import pytest

from app.services.inference.offline import analysis_child, analysis_proxy


class _FakeConn:
    def __init__(self, payload=None, eof=False):
        self.payload = payload
        self.eof = eof
        self.closed = False

    def poll(self, timeout=None):
        return self.payload is not None or self.eof

    def recv(self):
        if self.payload is None:
            raise EOFError
        payload, self.payload = self.payload, None
        return payload

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, pid=7, alive=False):
        self.pid = pid
        self.exitcode = None
        self.closed = False
        self.killed = False
        self.started = False
        self._alive = alive

    def start(self):
        self.started = True
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        return

    def kill(self):
        self.killed = True
        self._alive = False
        self.exitcode = -9

    def close(self):
        self.closed = True


class _FakeCtx:
    def __init__(self, proc, recv_conn, send_conn):
        self.proc = proc
        self.recv_conn = recv_conn
        self.send_conn = send_conn
        self.process_kwargs = None

    def Pipe(self, duplex):
        assert duplex is False
        return self.recv_conn, self.send_conn

    def Process(self, **kwargs):
        self.process_kwargs = kwargs
        return self.proc


def _patch_ctx(monkeypatch, ctx):
    monkeypatch.setattr(analysis_proxy.multiprocessing, "get_context", lambda method: ctx)


def test_spawn_wires_child_and_closes_parent_write_end(monkeypatch):
    proc = _FakeProc()
    recv_conn, send_conn = _FakeConn(), _FakeConn()
    ctx = _FakeCtx(proc, recv_conn, send_conn)
    _patch_ctx(monkeypatch, ctx)

    handle = analysis_proxy.spawn(1, 2, 3, 4)

    assert proc.started
    assert ctx.process_kwargs["target"] is analysis_child.worker_main
    assert ctx.process_kwargs["args"] == (send_conn, 1, 2, 3, 4)
    assert ctx.process_kwargs["daemon"] is True
    assert send_conn.closed  # 父进程不留写端，否则永远读不到 EOF
    assert handle.pid == proc.pid


def test_spawn_start_failure_closes_both_ends(monkeypatch):
    class _BoomProc(_FakeProc):
        def start(self):
            raise OSError("no fork")

    recv_conn, send_conn = _FakeConn(), _FakeConn()
    _patch_ctx(monkeypatch, _FakeCtx(_BoomProc(), recv_conn, send_conn))
    with pytest.raises(OSError, match="no fork"):
        analysis_proxy.spawn(1, 2, 3, 4)
    assert recv_conn.closed and send_conn.closed


def test_poll_result_returns_payload_once_then_caches():
    payload = {"status": "completed"}
    handle = analysis_proxy.WorkerProcess(_FakeProc(alive=True), _FakeConn(payload=payload))
    assert handle.poll_result(0.01) == payload
    assert handle.poll_result(0.0) == payload


def test_poll_result_timeout_returns_none():
    handle = analysis_proxy.WorkerProcess(_FakeProc(alive=True), _FakeConn())
    assert handle.poll_result(0.0) is None
    assert handle.at_eof is False


def test_eof_marks_at_eof():
    handle = analysis_proxy.WorkerProcess(_FakeProc(), _FakeConn(eof=True))
    assert handle.poll_result(0.0) is None
    assert handle.at_eof is True


def test_kill_only_when_alive():
    dead = _FakeProc(alive=False)
    analysis_proxy.WorkerProcess(dead, _FakeConn()).kill()
    assert dead.killed is False

    alive = _FakeProc(alive=True)
    analysis_proxy.WorkerProcess(alive, _FakeConn()).kill()
    assert alive.killed is True


def test_close_releases_conn_and_only_closes_exited_process():
    alive = _FakeProc(alive=True)
    conn = _FakeConn()
    analysis_proxy.WorkerProcess(alive, conn).close()
    assert conn.closed and alive.closed is False

    dead = _FakeProc(alive=False)
    analysis_proxy.WorkerProcess(dead, _FakeConn()).close()
    assert dead.closed is True
