"""`app.services.utils.process_group`：收尸顺序与容错。

断言只盯**换序就会重新引入挂死**的那几条：子进程先收（terminate → join → kill → join）、
读端在拆队列之前关、每条队列 cancel_join_thread → close。不 spawn 真进程。

三步法的由来见 docs/update/20260902_INFER_PROXY_EXIT_HANG.md。
"""

import logging
import queue

from app.services.utils.process_group import kill_process_group

_JOIN = 2.0  # 模块内的收尸宽限；断言它被原样传下去


class _Reader:
    def __init__(self, events, name):
        self._events = events
        self._name = name

    def close(self):
        self._events.append(f"{self._name}._reader.close")


class _Queue:
    """记录拆解动作的假队列。`fail_on` 用来模拟已经关过 / 非 multiprocessing 队列。"""

    def __init__(self, events, name, fail_on=()):
        self._events = events
        self._name = name
        self._fail_on = set(fail_on)
        self._reader = _Reader(events, name)

    def cancel_join_thread(self):
        if "cancel" in self._fail_on:
            raise ValueError("already cancelled")
        self._events.append(f"{self._name}.cancel_join_thread")

    def close(self):
        if "close" in self._fail_on:
            raise OSError("already closed")
        self._events.append(f"{self._name}.close")


class _Proc:
    """假进程：`dies_on` 指定哪一步之后 is_alive() 转 False（"never" = 杀不死）。"""

    def __init__(self, events, dies_on="terminate"):
        self._events = events
        self._dies_on = dies_on
        self._alive = True

    def is_alive(self):
        self._events.append("is_alive")
        return self._alive

    def terminate(self):
        self._events.append("terminate")
        if self._dies_on == "terminate":
            self._alive = False

    def join(self, timeout=None):
        self._events.append(("join", timeout))

    def kill(self):
        self._events.append("kill")
        if self._dies_on == "kill":
            self._alive = False


def _actions(events):
    return [e for e in events if e != "is_alive"]


def test_terminate_is_enough_when_child_exits():
    """terminate 后已退出就不发 SIGKILL。"""
    events = []
    proc, req, resp = _Proc(events), _Queue(events, "req"), _Queue(events, "resp")

    kill_process_group([proc], queues=(req, resp), break_reader_of=req)

    assert _actions(events) == [
        "terminate", ("join", _JOIN),
        "req._reader.close",
        "req.cancel_join_thread", "req.close",
        "resp.cancel_join_thread", "resp.close",
    ]


def test_kill_escalates_when_terminate_does_not_land():
    """SIGTERM 没送走（CUDA D 态）：再 SIGKILL，且仍给一次有界 join。"""
    events = []
    proc = _Proc(events, dies_on="kill")

    kill_process_group([proc])

    assert _actions(events) == ["terminate", ("join", _JOIN), "kill", ("join", _JOIN)]


def test_kill_does_not_land_but_queue_teardown_still_runs():
    """杀不死也往下走：队列收尾不能因进程收不掉而跳过（否则 atexit 仍会挂）。"""
    events = []
    proc, req = _Proc(events, dies_on="never"), _Queue(events, "req")

    kill_process_group([proc], queues=(req,), break_reader_of=req)

    assert "req._reader.close" in events
    assert "req.cancel_join_thread" in events
    assert "req.close" in events


def test_reader_closed_before_queues_closed():
    """读端必须先关：否则阻塞中的 feeder 拿不到 EPIPE，后面撤 join 只是让进程能退。"""
    events = []
    req = _Queue(events, "req")

    kill_process_group(procs=(), queues=(req,), break_reader_of=req)

    assert events == [
        "req._reader.close", "req.cancel_join_thread", "req.close",
    ]


def test_failures_are_swallowed_but_reported(caplog):
    """收尸路径不抛：进程句柄已关、队列已注销都只是日志一条，后面的步骤照跑。"""
    events = []

    class _BoomProc(_Proc):
        def is_alive(self):
            raise OSError("handle closed")

    proc = _BoomProc(events)
    req = _Queue(events, "req", fail_on={"cancel"})

    caplog.set_level(logging.WARNING, logger="tests.process_group")
    kill_process_group(
        [proc], queues=(req,), break_reader_of=req,
        logger=logging.getLogger("tests.process_group"), label="Fake",
    )

    assert "[Fake] kill child failed: handle closed" in caplog.text
    assert "req._reader.close" in events
    assert "req.close" in events  # cancel 失败不挡 close


def test_none_entries_and_plain_queue_are_tolerated():
    """`None` 跳过；`queue.Queue` 这种没有 `_reader` / `cancel_join_thread` 的也当已收干净。"""
    plain = queue.Queue()

    kill_process_group([None], queues=(None, plain), break_reader_of=plain)
    kill_process_group()

    assert plain.empty()
