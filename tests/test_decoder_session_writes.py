"""decoder 写帧：RAW 全帧率、LATEST_RAW 最新帧、READY 按整数倍率抽帧；首帧埋点每 run 一次。"""

import logging
from unittest.mock import MagicMock

import numpy as np
import pytest

from factories import make_session
from app.services.session.topics import LATEST_RAW, RAW, READY
from app.services.stream.config import DecoderConfig
from app.services.stream.decoder import FFmpegDecoder
from app.settings import settings


def _decoder(sess, decimation=3):
    settings_backup = settings.inference_decimation
    settings.inference_decimation = decimation
    try:
        manager = MagicMock()
        manager.get_pending_count.return_value = 0
        cfg = DecoderConfig(default_width=4, default_height=2, default_fps=30)
        return FFmpegDecoder(
            manager=manager, task_id=1, stream_url="rtsp://x", decoder_config=cfg, sess=sess,
        )
    finally:
        settings.inference_decimation = settings_backup


def _feed(dec, n):
    for _ in range(n):
        dec._process_frame(np.zeros((2, 4, 3), dtype=np.uint8))


def test_decimation_raw_and_latest(topic_sizes):
    topic_sizes(ca_maxlen=100, ca_ready_maxlen=100)
    sess = make_session()
    dec = _decoder(sess, decimation=3)

    _feed(dec, 9)

    assert len(sess.channel(RAW)) == 9
    assert len(sess.channel(READY)) == 3          # 每 3 帧留 1
    assert sess.channel(LATEST_RAW).get() is not None
    assert dec.frames_written_to_raw == 9 and dec.frames_written_to_ready == 3


def test_first_frame_logged_once_per_run(caplog, topic_sizes):
    topic_sizes(ca_maxlen=100, ca_ready_maxlen=100)
    sess = make_session()
    with caplog.at_level(logging.INFO, logger="app.startup_latency"):
        _feed(_decoder(sess), 2)
        _feed(_decoder(sess), 2)                    # 重连后新 decoder 写同一个 run，不重报

    lines = [r.getMessage() for r in caplog.records if "first_frame" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("[startup] task=1 step=1 first_frame +")


@pytest.mark.parametrize("seal", ["seal", "close"])
def test_sealed_run_takes_no_frames(seal, topic_sizes):
    topic_sizes(ca_maxlen=100, ca_ready_maxlen=100)
    sess = make_session()
    getattr(sess, seal)()
    dec = _decoder(sess, decimation=1)

    _feed(dec, 3)

    assert len(sess.channel(RAW)) == 0 and len(sess.channel(READY)) == 0
    assert sess.channel(LATEST_RAW).get() is None
    assert dec.frames_written_to_raw == 0
