"""StreamService.stop_stream 同步停：返回前 decoder.stop() 已完成，且不在全局锁内执行。"""

import threading
from unittest.mock import MagicMock

import pytest

from app.services.stream.service import StreamService

TID = 7


def test_stop_stream_stops_decoder_before_return_outside_lock():
    svc = StreamService()
    seen = {}

    def on_stop():
        seen["thread"] = threading.current_thread()
        got = svc.lock.acquire(blocking=False)
        seen["lock_free"] = got
        if got:
            svc.lock.release()

    dec = MagicMock(name="dec")
    dec.stop.side_effect = on_stop
    svc.decoders[TID] = dec

    svc.stop_stream(TID)

    dec.stop.assert_called_once()
    assert seen["thread"] is threading.current_thread()   # 调用线程内同步停，不另起线程
    assert seen["lock_free"] is True                       # 停时不持 StreamService.lock
    assert TID not in svc.decoders


def test_stop_stream_propagates_decoder_stop_error():
    """同步后 stop 的异常交给调用方（stop_run 记入 errors），不再被后台线程吞掉。"""
    svc = StreamService()
    dec = MagicMock(name="dec")
    dec.stop.side_effect = RuntimeError("boom")
    svc.decoders[TID] = dec

    with pytest.raises(RuntimeError):
        svc.stop_stream(TID)
    assert TID not in svc.decoders
