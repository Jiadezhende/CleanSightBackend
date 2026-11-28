"""离线作业服务：串行、去重、取消 / 停机 / 超时 kill、结果收口、spawn 接线与进程句柄。

子进程全用假件（doubles.FakeSpawner / 本文件的假 ctx & Connection）：用例手动 `finish()`，
不起真进程、不碰 torch。真 spawn 子进程的端到端验证见 integration_tests/test_offline_job_subprocess.py。
"""

import time
from types import SimpleNamespace

import pytest

from doubles import FakeSpawner, offline_result, wait_until

from app.types.run import RunIdentity
from app.services.inference.config import InferenceConfig
from app.services.inference.offline import analysis_child
from app.services.inference.offline import service as service_module
from app.services.inference.offline.service import THREADS, OfflineJobService, WorkerProcess
from app.types.exceptions import ConflictError, ValidationError

# 提交校验只看「step 在配置里且 offline 非空」，class 不会被 import（子进程才实例化）。
_CFG = InferenceConfig({"stages": {
    **{k: {"offline": {"class": "unused.Segmenter"}} for k in ("1", "2", "3", "987")},
    "5": {"detectors": [{"name": "x"}], "offline": {}},   # 有在线检测、无离线模型
}})


def _r(task_id, step_id, run_id=1):
    """服务只按值用 RunIdentity、不碰盘，测试直接构造。"""
    return RunIdentity(task_id, step_id, run_id)


class _Clients:
    """注册表替身：`get(task_id)` 返回挂在上面的 CQ。"""

    def __init__(self, registry=None):
        self.registry = dict(registry or {})

    def get(self, task_id):
        return self.registry.get(task_id)


@pytest.fixture
def env(fast_task_queue):
    spawner = FakeSpawner()
    clients = _Clients()
    svc = OfflineJobService(config=_CFG, spawner=spawner, sessions=clients, poll_s=0.02)
    svc.start()
    yield SimpleNamespace(svc=svc, spawner=spawner, clients=clients)
    svc.stop(timeout=5.0)


def _status(env, task_id=1, step_id=2):
    return env.svc.get(_r(task_id, step_id)).status


def _wait_spawned(env, n):
    wait_until(lambda: len(env.spawner.procs) >= n)
    return env.spawner.procs[n - 1]


class TestSerial:
    def test_runs_one_at_a_time_in_submit_order(self, env):
        env.svc.submit(_r(1, 2))
        env.svc.submit(_r(1, 3))
        first = _wait_spawned(env, 1)
        time.sleep(0.1)
        assert len(env.spawner.procs) == 1
        assert (_status(env, 1, 2), _status(env, 1, 3)) == ("running", "queued")

        first.finish(0, offline_result(alarm_count=2))
        second = _wait_spawned(env, 2)
        assert second.args[1] == 3
        second.finish(0, offline_result(segment_count=0))
        wait_until(lambda: _status(env, 1, 3) == "completed")

        job = env.svc.get(_r(1, 2))
        assert (job.status, job.producer, job.segment_count, job.alarm_count) == ("completed", "P", 3, 2)
        assert env.svc.get(_r(1, 3)).alarm_count is None
        assert job.started_at is not None and job.finished_at is not None

    def test_subprocess_gets_locked_run_and_threads(self, env):
        """子进程只收明确的 (task, step, run) 与线程数：不再拼命令行、不传配置对象。"""
        env.svc.submit(_r(1, 2))
        assert _wait_spawned(env, 1).args == (1, 2, 1, THREADS)

    def test_skipped_passes_through(self, env):
        env.svc.submit(_r(1, 2))
        _wait_spawned(env, 1).finish(0, offline_result(status="skipped", segment_count=0, message="m"))
        wait_until(lambda: _status(env) == "skipped")
        assert env.svc.get(_r(1, 2)).message == "m"


class TestSubmit:
    def test_running_run_is_409(self, env):
        """该 run 就是注册表里正在跑的那个：输入还在写，拒收且不留作业记录。"""
        env.clients.registry[1] = SimpleNamespace(info=SimpleNamespace(run=_r(1, 2)))
        with pytest.raises(ConflictError, match="正在运行"):
            env.svc.submit(_r(1, 2))
        assert env.svc.get(_r(1, 2)) is None
        assert env.spawner.procs == []

    def test_older_run_of_a_running_step_is_accepted(self, env):
        env.clients.registry[1] = SimpleNamespace(info=SimpleNamespace(run=_r(1, 2, run_id=2)))
        assert env.svc.submit(_r(1, 2, run_id=1)).status == "queued"

    def test_different_runs_of_one_step_are_separate_jobs(self, env):
        """去重键是 run：同 step 换代后再提交是新作业，显式点旧 run 也不会取消新 run 的作业。"""
        env.svc.submit(_r(1, 2, run_id=1))
        new = env.svc.submit(_r(1, 2, run_id=2))
        assert (new.run_id, new.status) == (2, "queued")
        first = _wait_spawned(env, 1)
        first.finish(0, offline_result())
        second = _wait_spawned(env, 2)
        assert second.args[2] == 2
        assert env.svc.get(_r(1, 2, run_id=1)).status == "completed"

    def test_reclaimed_passes_through(self, env):
        env.svc.submit(_r(1, 2))
        _wait_spawned(env, 1).finish(0, offline_result(status="reclaimed", segment_count=0, message="gone"))
        wait_until(lambda: _status(env) == "reclaimed")

    def test_duplicate_returns_in_flight_job(self, env):
        a = env.svc.submit(_r(1, 2))
        b = env.svc.submit(_r(1, 2))
        assert a.submitted_at == b.submitted_at
        _wait_spawned(env, 1).finish(0, offline_result())
        wait_until(lambda: _status(env) == "completed")
        time.sleep(0.05)
        assert len(env.spawner.procs) == 1

    def test_resubmit_after_finish_creates_new_job(self, env):
        env.svc.submit(_r(1, 2))
        _wait_spawned(env, 1).finish(0, offline_result())
        wait_until(lambda: _status(env) == "completed")
        assert env.svc.submit(_r(1, 2)).status == "queued"
        _wait_spawned(env, 2)

    @pytest.mark.parametrize("step_id,reason", [(99, "未在推理配置中定义"), (5, "未配置离线模型")])
    def test_unrunnable_step_rejected(self, env, step_id, reason):
        """未配置 / 无离线模型的 step 提交即 ValidationError（400），不入队、不留作业记录。"""
        with pytest.raises(ValidationError, match=reason):
            env.svc.submit(_r(1, step_id))
        assert env.svc.get(_r(1, step_id)) is None
        assert env.spawner.procs == []

    def test_not_started_rejected(self):
        with pytest.raises(ConflictError):
            OfflineJobService(config=_CFG, spawner=FakeSpawner()).submit(_r(1, 2))


class TestCancel:
    def test_cancel_queued_never_launches(self, env):
        env.svc.submit(_r(1, 1))
        blocker = _wait_spawned(env, 1)
        env.svc.submit(_r(1, 2))
        assert env.svc.cancel(_r(1, 2)) is True
        assert _status(env) == "cancelled"
        blocker.finish(0, offline_result())
        wait_until(lambda: _status(env, 1, 1) == "completed")
        time.sleep(0.05)
        assert len(env.spawner.procs) == 1

    def test_cancel_running_kills(self, env):
        env.svc.submit(_r(1, 2))
        proc = _wait_spawned(env, 1)
        assert env.svc.cancel(_r(1, 2)) is True
        wait_until(lambda: _status(env) == "cancelled")
        assert proc.killed

    def test_cancel_after_result_before_exit_still_cancels(self, env):
        """终止原因优先于子进程返回值：结果已到但进程未退出时取消，仍记 cancelled。"""
        env.svc.submit(_r(1, 2))
        proc = _wait_spawned(env, 1)
        proc.send_result(offline_result())
        assert env.svc.cancel(_r(1, 2)) is True
        wait_until(lambda: _status(env) == "cancelled")
        assert proc.killed

    def test_cancel_unknown_or_finished_is_false(self, env):
        assert env.svc.cancel(_r(9, 9)) is False
        env.svc.submit(_r(1, 2))
        _wait_spawned(env, 1).finish(0, offline_result())
        wait_until(lambda: _status(env) == "completed")
        assert env.svc.cancel(_r(1, 2)) is False


class TestFailures:
    def test_error_result_uses_its_message(self, env):
        env.svc.submit(_r(1, 2))
        _wait_spawned(env, 1).finish(1, {"status": "error", "producer": None,
                                         "segment_count": 0, "alarm_count": None, "message": "boom"})
        wait_until(lambda: _status(env) == "failed")
        assert env.svc.get(_r(1, 2)).message == "boom"

    def test_exit_without_result_failed(self, env):
        env.svc.submit(_r(1, 2))
        _wait_spawned(env, 1).finish(1, None)
        wait_until(lambda: _status(env) == "failed")
        message = env.svc.get(_r(1, 2)).message
        assert "exitcode=1" in message and "未回传结果" in message

    def test_zero_exit_without_result_failed(self, env):
        env.svc.submit(_r(1, 2))
        _wait_spawned(env, 1).finish(0, None)
        wait_until(lambda: _status(env) == "failed")

    def test_result_before_exit_waits_for_process(self, env):
        """通道里已有结果时进程仍算 running：必须等它正常退出才登记。"""
        env.svc.submit(_r(1, 2))
        proc = _wait_spawned(env, 1)
        proc.send_result(offline_result(segment_count=4))
        time.sleep(0.05)
        assert _status(env) == "running"
        proc.finish(0)
        wait_until(lambda: _status(env) == "completed")
        assert env.svc.get(_r(1, 2)).segment_count == 4

    def test_timeout_kills(self, fast_task_queue):
        spawner = FakeSpawner()
        svc = OfflineJobService(config=_CFG, spawner=spawner, poll_s=0.02, job_timeout_s=0.1)
        svc.start()
        try:
            svc.submit(_r(1, 2))
            wait_until(lambda: svc.get(_r(1, 2)).status == "failed")
            assert spawner.procs[0].killed
            assert "超时" in svc.get(_r(1, 2)).message
        finally:
            svc.stop(timeout=5.0)

    def test_timeout_wins_over_result(self, fast_task_queue):
        """结果先到但进程超时未退：终止原因（超时）优先，不登记 completed。"""
        spawner = FakeSpawner()
        svc = OfflineJobService(config=_CFG, spawner=spawner, poll_s=0.02, job_timeout_s=0.1)
        svc.start()
        try:
            svc.submit(_r(1, 2))
            wait_until(lambda: len(spawner.procs) == 1)
            spawner.procs[0].send_result(offline_result())
            wait_until(lambda: svc.get(_r(1, 2)).status == "failed")
            assert "超时" in svc.get(_r(1, 2)).message
            assert spawner.procs[0].killed
        finally:
            svc.stop(timeout=5.0)

    def test_launch_error_failed_and_queue_continues(self, env):
        def boom(*a, **k):
            raise OSError("no spawn")

        real = env.svc._spawner
        env.svc._spawner = boom
        env.svc.submit(_r(1, 2))
        wait_until(lambda: _status(env) == "failed")
        assert "no spawn" in env.svc.get(_r(1, 2)).message
        env.svc._spawner = real
        env.svc.submit(_r(1, 3))
        _wait_spawned(env, 1)


class TestStop:
    def test_stop_kills_running_and_cancels_queued(self, fast_task_queue):
        spawner = FakeSpawner()
        svc = OfflineJobService(config=_CFG, spawner=spawner, poll_s=0.02)
        svc.start()
        svc.submit(_r(1, 1))
        svc.submit(_r(1, 2))
        wait_until(lambda: len(spawner.procs) == 1)
        started = time.monotonic()
        svc.stop(timeout=5.0)
        assert time.monotonic() - started < 2.0
        assert spawner.procs[0].killed
        assert (svc.get(_r(1, 1)).status, svc.get(_r(1, 2)).status) == ("cancelled", "cancelled")
        assert len(spawner.procs) == 1

    def test_restart_after_stop(self, fast_task_queue):
        svc = OfflineJobService(config=_CFG, spawner=FakeSpawner(), poll_s=0.02)
        svc.start()
        svc.stop()
        svc.start()
        try:
            assert svc.submit(_r(1, 2)).status == "queued"
        finally:
            svc.stop(timeout=5.0)


# ── spawn 接线与进程句柄（假 ctx / Connection / Process，不起真进程）──────────────


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
    monkeypatch.setattr(service_module.multiprocessing, "get_context", lambda method: ctx)


def test_spawn_wires_child_and_closes_parent_write_end(monkeypatch):
    proc = _FakeProc()
    recv_conn, send_conn = _FakeConn(), _FakeConn()
    ctx = _FakeCtx(proc, recv_conn, send_conn)
    _patch_ctx(monkeypatch, ctx)

    handle = service_module._spawn(1, 2, 3, 4)

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
        service_module._spawn(1, 2, 3, 4)
    assert recv_conn.closed and send_conn.closed


def test_poll_result_returns_payload_once_then_caches():
    payload = {"status": "completed"}
    handle = WorkerProcess(_FakeProc(alive=True), _FakeConn(payload=payload))
    assert handle.poll_result(0.01) == payload
    assert handle.poll_result(0.0) == payload


def test_poll_result_timeout_returns_none():
    handle = WorkerProcess(_FakeProc(alive=True), _FakeConn())
    assert handle.poll_result(0.0) is None
    assert handle.at_eof is False


def test_eof_marks_at_eof():
    handle = WorkerProcess(_FakeProc(), _FakeConn(eof=True))
    assert handle.poll_result(0.0) is None
    assert handle.at_eof is True


def test_kill_only_when_alive():
    dead = _FakeProc(alive=False)
    WorkerProcess(dead, _FakeConn()).kill()
    assert dead.killed is False

    alive = _FakeProc(alive=True)
    WorkerProcess(alive, _FakeConn()).kill()
    assert alive.killed is True


def test_close_releases_conn_and_only_closes_exited_process():
    alive = _FakeProc(alive=True)
    conn = _FakeConn()
    WorkerProcess(alive, conn).close()
    assert conn.closed and alive.closed is False

    dead = _FakeProc(alive=False)
    WorkerProcess(dead, _FakeConn()).close()
    assert dead.closed is True
