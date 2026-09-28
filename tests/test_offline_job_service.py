"""离线作业服务：串行、去重、取消 / 停机 / 超时 kill、结果解析。

子进程全用假件（doubles.FakeLauncher）：FakeProc 由用例手动 `finish()`，不起真进程、不碰 torch。
真起 CLI 子进程的端到端验证见 integration_tests/test_offline_job_subprocess.py。
"""

import time
from types import SimpleNamespace

import pytest

from doubles import FakeLauncher, offline_result, wait_until

from app.types.run import RunIdentity
from app.services.inference.config import InferenceConfig
from app.services.inference.offline.service import OfflineJobService
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
    launcher = FakeLauncher()
    clients = _Clients()
    svc = OfflineJobService(config=_CFG, launcher=launcher, clients=clients, poll_s=0.02)
    svc.start()
    yield SimpleNamespace(svc=svc, launcher=launcher, clients=clients)
    svc.stop(timeout=5.0)


def _status(env, task_id=1, step_id=2):
    return env.svc.get(_r(task_id, step_id)).status


def _wait_launched(env, n):
    wait_until(lambda: len(env.launcher.procs) >= n)
    return env.launcher.procs[n - 1]


class TestSerial:
    def test_runs_one_at_a_time_in_submit_order(self, env):
        env.svc.submit(_r(1, 2))
        env.svc.submit(_r(1, 3))
        first = _wait_launched(env, 1)
        time.sleep(0.1)
        assert len(env.launcher.procs) == 1
        assert (_status(env, 1, 2), _status(env, 1, 3)) == ("running", "queued")

        first.finish(0, offline_result())
        second = _wait_launched(env, 2)
        assert "--step-id" in second.cmd and second.cmd[second.cmd.index("--step-id") + 1] == "3"
        second.finish(0, offline_result(segment_count=0))
        wait_until(lambda: _status(env, 1, 3) == "completed")

        job = env.svc.get(_r(1, 2))
        assert (job.status, job.producer, job.segment_count) == ("completed", "P", 3)
        assert job.started_at is not None and job.finished_at is not None

    def test_command_is_cli_run(self, env):
        env.svc.submit(_r(1, 2))
        cmd = _wait_launched(env, 1).cmd
        assert "app.services.inference.offline.cli" in cmd
        assert cmd[cmd.index("run"):cmd.index("run") + 7] == [
            "run", "--task-id", "1", "--step-id", "2", "--run-id", "1",
        ]

    def test_skipped_passes_through(self, env):
        env.svc.submit(_r(1, 2))
        _wait_launched(env, 1).finish(0, offline_result(status="skipped", segment_count=0, message="m"))
        wait_until(lambda: _status(env) == "skipped")
        assert env.svc.get(_r(1, 2)).message == "m"


class TestSubmit:
    def test_running_run_is_409(self, env):
        """该 run 就是注册表里正在跑的那个：输入还在写，拒收且不留作业记录。"""
        env.clients.registry[1] = SimpleNamespace(run=_r(1, 2))
        with pytest.raises(ConflictError, match="正在运行"):
            env.svc.submit(_r(1, 2))
        assert env.svc.get(_r(1, 2)) is None
        assert env.launcher.procs == []

    def test_older_run_of_a_running_step_is_accepted(self, env):
        env.clients.registry[1] = SimpleNamespace(run=_r(1, 2, run_id=2))
        assert env.svc.submit(_r(1, 2, run_id=1)).status == "queued"

    def test_different_runs_of_one_step_are_separate_jobs(self, env):
        """去重键是 run：同 step 换代后再提交是新作业，显式点旧 run 也不会取消新 run 的作业。"""
        env.svc.submit(_r(1, 2, run_id=1))
        new = env.svc.submit(_r(1, 2, run_id=2))
        assert (new.run_id, new.status) == (2, "queued")
        first = _wait_launched(env, 1)
        first.finish(0, offline_result())
        second = _wait_launched(env, 2)
        assert second.cmd[second.cmd.index("--run-id") + 1] == "2"
        assert env.svc.get(_r(1, 2, run_id=1)).status == "completed"

    def test_reclaimed_passes_through(self, env):
        env.svc.submit(_r(1, 2))
        _wait_launched(env, 1).finish(0, offline_result(status="reclaimed", segment_count=0, message="gone"))
        wait_until(lambda: _status(env) == "reclaimed")

    def test_duplicate_returns_in_flight_job(self, env):
        a = env.svc.submit(_r(1, 2))
        b = env.svc.submit(_r(1, 2))
        assert a.submitted_at == b.submitted_at
        _wait_launched(env, 1).finish(0, offline_result())
        wait_until(lambda: _status(env) == "completed")
        time.sleep(0.05)
        assert len(env.launcher.procs) == 1

    def test_resubmit_after_finish_creates_new_job(self, env):
        env.svc.submit(_r(1, 2))
        _wait_launched(env, 1).finish(0, offline_result())
        wait_until(lambda: _status(env) == "completed")
        assert env.svc.submit(_r(1, 2)).status == "queued"
        _wait_launched(env, 2)

    @pytest.mark.parametrize("step_id,reason", [(99, "未在推理配置中定义"), (5, "未配置离线模型")])
    def test_unrunnable_step_rejected(self, env, step_id, reason):
        """未配置 / 无离线模型的 step 提交即 ValidationError（400），不入队、不留作业记录。"""
        with pytest.raises(ValidationError, match=reason):
            env.svc.submit(_r(1, step_id))
        assert env.svc.get(_r(1, step_id)) is None
        assert env.launcher.procs == []

    def test_not_started_rejected(self):
        with pytest.raises(ConflictError):
            OfflineJobService(config=_CFG, launcher=FakeLauncher()).submit(_r(1, 2))


class TestCancel:
    def test_cancel_queued_never_launches(self, env):
        env.svc.submit(_r(1, 1))
        blocker = _wait_launched(env, 1)
        env.svc.submit(_r(1, 2))
        assert env.svc.cancel(_r(1, 2)) is True
        assert _status(env) == "cancelled"
        blocker.finish(0, offline_result())
        wait_until(lambda: _status(env, 1, 1) == "completed")
        time.sleep(0.05)
        assert len(env.launcher.procs) == 1

    def test_cancel_running_kills(self, env):
        env.svc.submit(_r(1, 2))
        proc = _wait_launched(env, 1)
        assert env.svc.cancel(_r(1, 2)) is True
        wait_until(lambda: _status(env) == "cancelled")
        assert proc.killed

    def test_cancel_unknown_or_finished_is_false(self, env):
        assert env.svc.cancel(_r(9, 9)) is False
        env.svc.submit(_r(1, 2))
        _wait_launched(env, 1).finish(0, offline_result())
        wait_until(lambda: _status(env) == "completed")
        assert env.svc.cancel(_r(1, 2)) is False


class TestFailures:
    def test_nonzero_exit_uses_json_message(self, env):
        env.svc.submit(_r(1, 2))
        _wait_launched(env, 1).finish(1, {"status": "error", "producer": None,
                                          "segment_count": 0, "message": "boom"})
        wait_until(lambda: _status(env) == "failed")
        assert env.svc.get(_r(1, 2)).message == "boom"

    def test_nonzero_exit_without_json_uses_stderr_tail(self, env):
        env.svc.submit(_r(1, 2))
        _wait_launched(env, 1).finish(1, None, stderr=b"Traceback ...\nImportError: x\n")
        wait_until(lambda: _status(env) == "failed")
        assert env.svc.get(_r(1, 2)).message.endswith("ImportError: x")

    def test_zero_exit_without_json_failed(self, env):
        env.svc.submit(_r(1, 2))
        _wait_launched(env, 1).finish(0, None)
        wait_until(lambda: _status(env) == "failed")

    def test_timeout_kills(self, fast_task_queue):
        launcher = FakeLauncher()
        svc = OfflineJobService(config=_CFG, launcher=launcher, poll_s=0.02, job_timeout_s=0.1)
        svc.start()
        try:
            svc.submit(_r(1, 2))
            wait_until(lambda: svc.get(_r(1, 2)).status == "failed")
            assert launcher.procs[0].killed
            assert "超时" in svc.get(_r(1, 2)).message
        finally:
            svc.stop(timeout=5.0)

    def test_launch_error_failed_and_queue_continues(self, env):
        def boom(*a, **k):
            raise OSError("no python")

        real = env.svc._launcher
        env.svc._launcher = boom
        env.svc.submit(_r(1, 2))
        wait_until(lambda: _status(env) == "failed")
        assert "no python" in env.svc.get(_r(1, 2)).message
        env.svc._launcher = real
        env.svc.submit(_r(1, 3))
        _wait_launched(env, 1)


class TestStop:
    def test_stop_kills_running_and_cancels_queued(self, fast_task_queue):
        launcher = FakeLauncher()
        svc = OfflineJobService(config=_CFG, launcher=launcher, poll_s=0.02)
        svc.start()
        svc.submit(_r(1, 1))
        svc.submit(_r(1, 2))
        wait_until(lambda: len(launcher.procs) == 1)
        started = time.monotonic()
        svc.stop(timeout=5.0)
        assert time.monotonic() - started < 2.0
        assert launcher.procs[0].killed
        assert (svc.get(_r(1, 1)).status, svc.get(_r(1, 2)).status) == ("cancelled", "cancelled")
        assert len(launcher.procs) == 1

    def test_restart_after_stop(self, fast_task_queue):
        svc = OfflineJobService(config=_CFG, launcher=FakeLauncher(), poll_s=0.02)
        svc.start()
        svc.stop()
        svc.start()
        try:
            assert svc.submit(_r(1, 2)).status == "queued"
        finally:
            svc.stop(timeout=5.0)
