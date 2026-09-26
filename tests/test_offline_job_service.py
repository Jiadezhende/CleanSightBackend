"""离线作业服务：串行、去重、live 拦截、取消 / 停机 / 超时 / 换代 kill、结果解析。

子进程与 client 注册表全用假件：FakeProc 由用例手动 `finish()`，不起真进程、不碰 torch。
"""

import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from app.services.inference.config import InferenceConfig
from app.services.inference.offline.service import OfflineJobService
from app.utils.exceptions import ConflictError, ValidationError

# 提交校验只看「step 在配置里且 offline 非空」，class 不会被 import（子进程才实例化）。
_CFG = InferenceConfig({"stages": {
    **{k: {"offline": {"class": "unused.Segmenter"}} for k in ("1", "2", "3", "987")},
    "5": {"detectors": [{"name": "x"}], "offline": {}},   # 有在线检测、无离线模型
}})


class FakeProc:
    def __init__(self, cmd, stdout, stderr):
        self.cmd = cmd
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self._stdout = stdout
        self._stderr = stderr
        self._done = threading.Event()

    def finish(self, returncode=0, result=None, stderr=b""):
        if result is not None:
            self._stdout.write(b"log line\n" + json.dumps(result).encode() + b"\n")
        self._stderr.write(stderr)
        self.returncode = returncode
        self._done.set()

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.finish(returncode=-9)


class FakeLauncher:
    def __init__(self):
        self.procs = []

    def __call__(self, cmd, *, stdout, stderr, **kwargs):
        proc = FakeProc(cmd, stdout, stderr)
        self.procs.append(proc)
        return proc


class FakeClients:
    """task_id → 带 step_id 的假 CQ；空 = 没有 live run。"""

    def __init__(self):
        self.runs = {}

    def get(self, task_id):
        return self.runs.get(task_id)

    def go_live(self, task_id, step_id):
        self.runs[task_id] = SimpleNamespace(step_id=step_id)


def _ok(status="completed", producer="P", segment_count=3, message=""):
    return {"status": status, "producer": producer, "segment_count": segment_count, "message": message}


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("条件未在时限内满足")


@pytest.fixture
def env():
    clients, launcher = FakeClients(), FakeLauncher()
    svc = OfflineJobService(config=_CFG, clients=clients, launcher=launcher, poll_s=0.02)
    svc.start()
    yield SimpleNamespace(svc=svc, clients=clients, launcher=launcher)
    svc.stop(timeout=5.0)


def _status(env, task_id=1, step_id=2):
    return env.svc.get(task_id, step_id).status


def _wait_launched(env, n):
    _wait_until(lambda: len(env.launcher.procs) >= n)
    return env.launcher.procs[n - 1]


class TestSerial:
    def test_runs_one_at_a_time_in_submit_order(self, env):
        env.svc.submit(1, 2)
        env.svc.submit(1, 3)
        first = _wait_launched(env, 1)
        time.sleep(0.1)
        assert len(env.launcher.procs) == 1
        assert (_status(env, 1, 2), _status(env, 1, 3)) == ("running", "queued")

        first.finish(0, _ok())
        second = _wait_launched(env, 2)
        assert "--step-id" in second.cmd and second.cmd[second.cmd.index("--step-id") + 1] == "3"
        second.finish(0, _ok(segment_count=0))
        _wait_until(lambda: _status(env, 1, 3) == "completed")

        job = env.svc.get(1, 2)
        assert (job.status, job.producer, job.segment_count) == ("completed", "P", 3)
        assert job.started_at is not None and job.finished_at is not None

    def test_command_is_cli_run(self, env):
        env.svc.submit(1, 2)
        cmd = _wait_launched(env, 1).cmd
        assert "app.services.inference.offline.cli" in cmd
        assert cmd[cmd.index("run"):cmd.index("run") + 5] == ["run", "--task-id", "1", "--step-id", "2"]

    @pytest.mark.parametrize("status", ["skipped", "superseded"])
    def test_runner_statuses_pass_through(self, env, status):
        env.svc.submit(1, 2)
        _wait_launched(env, 1).finish(0, _ok(status=status, segment_count=0, message="m"))
        _wait_until(lambda: _status(env) == status)
        assert env.svc.get(1, 2).message == "m"


class TestSubmit:
    def test_duplicate_returns_in_flight_job(self, env):
        a = env.svc.submit(1, 2)
        b = env.svc.submit(1, 2)
        assert a.submitted_at == b.submitted_at
        _wait_launched(env, 1).finish(0, _ok())
        _wait_until(lambda: _status(env) == "completed")
        time.sleep(0.05)
        assert len(env.launcher.procs) == 1

    def test_resubmit_after_finish_creates_new_job(self, env):
        env.svc.submit(1, 2)
        _wait_launched(env, 1).finish(0, _ok())
        _wait_until(lambda: _status(env) == "completed")
        assert env.svc.submit(1, 2).status == "queued"
        _wait_launched(env, 2)

    @pytest.mark.parametrize("step_id,reason", [(99, "未在推理配置中定义"), (5, "未配置离线模型")])
    def test_unrunnable_step_rejected(self, env, step_id, reason):
        """未配置 / 无离线模型的 step 提交即 ValidationError（400），不入队、不留作业记录。"""
        with pytest.raises(ValidationError, match=reason):
            env.svc.submit(1, step_id)
        assert env.svc.get(1, step_id) is None
        assert env.launcher.procs == []

    def test_live_step_rejected(self, env):
        env.clients.go_live(1, 2)
        with pytest.raises(ConflictError):
            env.svc.submit(1, 2)
        assert env.svc.get(1, 2) is None

    def test_other_step_of_live_task_accepted(self, env):
        env.clients.go_live(1, 3)
        env.svc.submit(1, 2)
        _wait_launched(env, 1)

    def test_not_started_rejected(self):
        with pytest.raises(ConflictError):
            OfflineJobService(config=_CFG, clients=FakeClients(), launcher=FakeLauncher()).submit(1, 2)


class TestLive:
    def test_live_before_start_skipped_without_launch(self, env):
        env.svc.submit(1, 1)
        blocker = _wait_launched(env, 1)
        env.svc.submit(1, 2)
        env.clients.go_live(1, 2)
        blocker.finish(0, _ok())
        _wait_until(lambda: _status(env) == "skipped")
        assert len(env.launcher.procs) == 1

    def test_live_while_running_kills_superseded(self, env):
        env.svc.submit(1, 2)
        proc = _wait_launched(env, 1)
        env.clients.go_live(1, 2)
        _wait_until(lambda: _status(env) == "superseded")
        assert proc.killed


class TestCancel:
    def test_cancel_queued_never_launches(self, env):
        env.svc.submit(1, 1)
        blocker = _wait_launched(env, 1)
        env.svc.submit(1, 2)
        assert env.svc.cancel(1, 2) is True
        assert _status(env) == "cancelled"
        blocker.finish(0, _ok())
        _wait_until(lambda: _status(env, 1, 1) == "completed")
        time.sleep(0.05)
        assert len(env.launcher.procs) == 1

    def test_cancel_running_kills(self, env):
        env.svc.submit(1, 2)
        proc = _wait_launched(env, 1)
        assert env.svc.cancel(1, 2) is True
        _wait_until(lambda: _status(env) == "cancelled")
        assert proc.killed

    def test_cancel_unknown_or_finished_is_false(self, env):
        assert env.svc.cancel(9, 9) is False
        env.svc.submit(1, 2)
        _wait_launched(env, 1).finish(0, _ok())
        _wait_until(lambda: _status(env) == "completed")
        assert env.svc.cancel(1, 2) is False


class TestFailures:
    def test_nonzero_exit_uses_json_message(self, env):
        env.svc.submit(1, 2)
        _wait_launched(env, 1).finish(1, {"status": "error", "producer": None,
                                          "segment_count": 0, "message": "boom"})
        _wait_until(lambda: _status(env) == "failed")
        assert env.svc.get(1, 2).message == "boom"

    def test_nonzero_exit_without_json_uses_stderr_tail(self, env):
        env.svc.submit(1, 2)
        _wait_launched(env, 1).finish(1, None, stderr=b"Traceback ...\nImportError: x\n")
        _wait_until(lambda: _status(env) == "failed")
        assert env.svc.get(1, 2).message.endswith("ImportError: x")

    def test_zero_exit_without_json_failed(self, env):
        env.svc.submit(1, 2)
        _wait_launched(env, 1).finish(0, None)
        _wait_until(lambda: _status(env) == "failed")

    def test_timeout_kills(self):
        clients, launcher = FakeClients(), FakeLauncher()
        svc = OfflineJobService(config=_CFG, clients=clients, launcher=launcher, poll_s=0.02, job_timeout_s=0.1)
        svc.start()
        try:
            svc.submit(1, 2)
            _wait_until(lambda: svc.get(1, 2).status == "failed")
            assert launcher.procs[0].killed
            assert "超时" in svc.get(1, 2).message
        finally:
            svc.stop(timeout=5.0)

    def test_launch_error_failed_and_queue_continues(self, env):
        def boom(*a, **k):
            raise OSError("no python")

        real = env.svc._launcher
        env.svc._launcher = boom
        env.svc.submit(1, 2)
        _wait_until(lambda: _status(env) == "failed")
        assert "no python" in env.svc.get(1, 2).message
        env.svc._launcher = real
        env.svc.submit(1, 3)
        _wait_launched(env, 1)


class TestStop:
    def test_stop_kills_running_and_cancels_queued(self):
        clients, launcher = FakeClients(), FakeLauncher()
        svc = OfflineJobService(config=_CFG, clients=clients, launcher=launcher, poll_s=0.02)
        svc.start()
        svc.submit(1, 1)
        svc.submit(1, 2)
        _wait_until(lambda: len(launcher.procs) == 1)
        started = time.monotonic()
        svc.stop(timeout=5.0)
        assert time.monotonic() - started < 2.0
        assert launcher.procs[0].killed
        assert (svc.get(1, 1).status, svc.get(1, 2).status) == ("cancelled", "cancelled")
        assert len(launcher.procs) == 1

    def test_restart_after_stop(self):
        svc = OfflineJobService(config=_CFG, clients=FakeClients(), launcher=FakeLauncher(), poll_s=0.02)
        svc.start()
        svc.stop()
        svc.start()
        try:
            assert svc.submit(1, 2).status == "queued"
        finally:
            svc.stop(timeout=5.0)


class TestRealSubprocess:
    def test_end_to_end_with_real_cli(self, tmp_storage, monkeypatch):
        """真起 CLI 子进程：存储根经环境变量传给子进程；子进程读真实 YAML，987 未配置 → 退出非 0 → failed。

        服务侧注入的配置放行 987，子进程侧真实配置拒绝——不跑模型（不碰权重），只验证
        起进程 / env / cwd / 末行 JSON 解析 这条管线。
        """
        from factories import make_detector_output, make_frame_detection
        from app.storage import inference as inference_store

        monkeypatch.setenv("CLEANSIGHT_STORAGE_DIR", str(tmp_storage))
        inference_store.append_detections(1, 987, [
            make_frame_detection(ts=1.0, by_source={"x": make_detector_output(n=1, ts=1.0)})
        ])
        svc = OfflineJobService(config=_CFG, clients=FakeClients(), poll_s=0.05)
        svc.start()
        try:
            svc.submit(1, 987)
            _wait_until(lambda: svc.get(1, 987).status not in ("queued", "running"), timeout=60.0)
            job = svc.get(1, 987)
            assert job.status == "failed", job.message
            assert "987" in job.message and "未在推理配置中定义" in job.message
        finally:
            svc.stop(timeout=5.0)
