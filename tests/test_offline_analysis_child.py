"""离线分析子进程侧：运行环境初始化顺序、结果转换、结果回传与退出码。不起真进程、不碰 torch。"""

import os

import pytest

from app.services.inference.offline import analysis_child, worker as worker_mod
from app.services.inference.offline.worker import OfflineRunResult


def _block_configuration(monkeypatch):
    """拦掉钉卡/限线程（pytest 进程里不起真 torch）。"""
    monkeypatch.setattr(analysis_child, "configure_process", lambda num_threads: None)


class _RecordingWorker:
    """按用例给定的结果 / 异常跑一次 run，并记下 spec。"""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        if self.exc is not None:
            raise self.exc
        return self.result


def _install_worker(monkeypatch, worker):
    monkeypatch.setattr(worker_mod, "OfflineWorker", lambda: worker)


def test_execute_configures_process_then_returns_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(analysis_child, "configure_process", lambda num_threads: calls.append(num_threads))
    worker = _RecordingWorker(
        OfflineRunResult(status="completed", producer="P", segment_count=3, message="m", run_id=7, alarm_count=2)
    )
    _install_worker(monkeypatch, worker)

    payload = analysis_child.execute(1, 2, 3, 4)

    assert calls == [4]  # 先配置运行环境，再碰 worker
    assert (worker.specs[0].task_id, worker.specs[0].step_id, worker.specs[0].run_id) == (1, 2, 3)
    assert payload == {
        "status": "completed", "producer": "P", "segment_count": 3, "alarm_count": 2, "message": "m",
    }


def test_execute_converts_exception_to_error_payload(monkeypatch):
    _block_configuration(monkeypatch)
    _install_worker(monkeypatch, _RecordingWorker(exc=ValueError("boom")))

    assert analysis_child.execute(1, 2, None, 2) == {
        "status": "error", "producer": None, "segment_count": 0, "alarm_count": None, "message": "boom",
    }


def test_pin_cuda_device_uses_online_inference_card(monkeypatch):
    """离线子进程与在线推理子进程钉同一张卡（settings.cuda_device），不碰机器上别的卡。"""
    from app.settings import settings

    monkeypatch.setattr(settings, "cuda_device", "1")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1,2")
    analysis_child._pin_cuda_device()
    assert os.environ["CUDA_VISIBLE_DEVICES"] == "1"


class _FakeConn:
    def __init__(self, fail=False):
        self.payloads = []
        self.closed = False
        self.fail = fail

    def send(self, payload):
        if self.fail:
            raise BrokenPipeError("gone")
        self.payloads.append(payload)

    def close(self):
        self.closed = True


def _stub_worker_main(monkeypatch, status="completed"):
    # 子进程初始化（signal / chdir / 优先级）在 pytest 主进程里不可执行，替换掉
    monkeypatch.setattr(analysis_child, "_configure_child_process", lambda: None)
    monkeypatch.setattr(
        analysis_child, "execute",
        lambda *a: {"status": status, "producer": "P", "segment_count": 1, "alarm_count": None, "message": ""},
    )


def test_worker_main_sends_payload_and_exits_zero(monkeypatch):
    _stub_worker_main(monkeypatch)
    conn = _FakeConn()
    with pytest.raises(SystemExit) as exc:
        analysis_child.worker_main(conn, 1, 2, 3, 4)
    assert exc.value.code == 0
    assert conn.payloads[0]["status"] == "completed"
    assert conn.closed


def test_worker_main_exits_nonzero_on_error(monkeypatch):
    _stub_worker_main(monkeypatch, status="error")
    conn = _FakeConn()
    with pytest.raises(SystemExit) as exc:
        analysis_child.worker_main(conn, 1, 2, 3, 4)
    assert exc.value.code == 1


def test_worker_main_send_failure_does_not_raise(monkeypatch):
    _stub_worker_main(monkeypatch)
    conn = _FakeConn(fail=True)
    with pytest.raises(SystemExit) as exc:
        analysis_child.worker_main(conn, 1, 2, 3, 4)
    assert exc.value.code == 0
    assert conn.closed


def test_lower_priority_failure_is_non_fatal(monkeypatch):
    """降优先级是尽力而为：当前平台分支失败（权限 / 原型 / 缺 API）不能影响作业。"""

    def boom(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(analysis_child, "_lower_priority_windows", boom)
    monkeypatch.setattr(analysis_child, "_lower_priority_posix", boom)

    analysis_child._lower_priority()  # 不抛
