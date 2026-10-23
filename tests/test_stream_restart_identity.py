"""StreamService.restart_stream 的乐观核对：锁外停旧 decoder 期间槽位被并发改过 → 放弃。

restart 两段持 `StreamService.lock`，中间锁外同步停旧 decoder。用旧 decoder 的 `stop()`
副作用模拟这段空档里的并发 start / stop，断言 restart 不弹走、不复活别人的 decoder。
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.stream.service import StreamService

TID = 7
URL = "rtsp://127.0.0.1:18004/live"


@pytest.fixture
def svc():
    s = StreamService()
    with patch("app.services.stream.service.FFmpegDecoder") as MockDecoder, \
         patch("app.services.stream.service.client_service") as mock_cs:
        mock_cs.has_client.return_value = True
        mock_cs.get.return_value = MagicMock()
        s.MockDecoder = MockDecoder
        yield s


def _old_decoder(svc, on_stop=None):
    old = MagicMock(name="old_dec")
    if on_stop is not None:
        old.stop.side_effect = on_stop
    svc.decoders[TID] = old
    return old


def test_restart_replaces_decoder_when_slot_unchanged(svc):
    old = _old_decoder(svc)

    assert svc.restart_stream(TID, URL) is True

    old.stop.assert_called_once()
    assert svc.decoders[TID] is svc.MockDecoder.return_value
    svc.MockDecoder.return_value.start.assert_called_once()


def test_restart_skips_when_concurrent_start_replaced_slot(svc):
    intruder = MagicMock(name="intruder_dec")
    _old_decoder(svc, on_stop=lambda: svc.decoders.__setitem__(TID, intruder))

    assert svc.restart_stream(TID, URL) is False

    assert svc.decoders[TID] is intruder          # 没被弹走（弹走即漏停：无人再管它）
    intruder.stop.assert_not_called()
    svc.MockDecoder.assert_not_called()


def test_restart_skips_when_concurrent_stop_cleared_slot(svc):
    _old_decoder(svc, on_stop=lambda: svc.decoders.pop(TID, None))

    assert svc.restart_stream(TID, URL) is False

    assert TID not in svc.decoders                # stop 之后不被 restart 复活
    svc.MockDecoder.assert_not_called()
