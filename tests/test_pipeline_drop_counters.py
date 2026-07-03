"""推理链路静默丢帧计数器测试。

覆盖两个新增计数点：
- StageAwareDispatcher._stage_queues 满（maxlen）时静默淘汰 → get_stage_drops()
- ClientQueues.ca_processed 满（maxlen）时静默淘汰 → frames_dropped_processed
"""

import numpy as np
from unittest.mock import MagicMock, patch

from app.domain.frame import Frame
from app.services.client.queues import ClientQueues
from app.services.inference.detection.dispatcher import StageAwareDispatcher


def _frame() -> Frame:
    return Frame(timestamp=0.0, frame=np.zeros((2, 2, 3), dtype=np.uint8))


def test_stage_queue_drop_counted_when_full():
    """_stage_queues 已满时再 dispatch 一帧，应记一次 stage 丢帧。"""
    cm = MagicMock()
    dispatcher = StageAwareDispatcher(client_manager_instance=cm)

    # 客户端：ca_ready 有一帧，stage=MOCK（ClientQueues 默认 initial_stage）
    cq = ClientQueues(ca_maxlen=10)
    cq.ca_ready.append(_frame())
    cm.snapshot.return_value = {"c1": cq}

    # 预填满该 stage 队列到 maxlen
    stage = cq.get_stage()
    q = dispatcher._stage_queues[stage]
    for _ in range(q.maxlen):
        q.append(MagicMock())

    dispatcher._fetch_and_dispatch_round()

    assert dispatcher.get_stage_drops()[stage] == 1


def test_stage_queue_no_drop_when_not_full():
    """队列未满时 dispatch 不应记丢帧。"""
    cm = MagicMock()
    dispatcher = StageAwareDispatcher(client_manager_instance=cm)

    cq = ClientQueues(ca_maxlen=10)
    cq.ca_ready.append(_frame())
    cm.snapshot.return_value = {"c1": cq}

    dispatcher._fetch_and_dispatch_round()

    assert dispatcher.get_stage_drops().get(cq.get_stage(), 0) == 0


def test_ca_processed_drop_counted_on_overflow():
    """未绑定任务时 ca_processed 只进不出，超过 maxlen 的部分应被计数。"""
    cq = ClientQueues(ca_maxlen=3)
    assert cq.get_ca_processed_capacity() == 3

    for _ in range(5):
        cq.append_ca_processed(_frame())

    # 前 3 帧填满，后 2 帧触发淘汰计数
    assert cq.frames_dropped_processed == 2
    assert cq.get_ca_processed_length() == 3


def test_pressure_snapshot_silent_when_calm():
    """平稳（无丢帧、队列浅）时不应打印 [INFER_PRESSURE]，避免刷屏。"""
    cm = MagicMock()
    cm.snapshot.return_value = {}
    dispatcher = StageAwareDispatcher(client_manager_instance=cm)

    with patch("app.services.inference.detection.dispatcher.logger") as log:
        dispatcher._log_pressure_snapshot()

    log.info.assert_not_called()


def test_pressure_snapshot_logs_on_drop():
    """有新增丢帧时应打印一行 [INFER_PRESSURE]。"""
    cm = MagicMock()
    cm.snapshot.return_value = {}
    dispatcher = StageAwareDispatcher(client_manager_instance=cm)
    dispatcher._stage_drops["CLEAN"] = 5  # 自上次报告以来新增丢帧

    with patch("app.services.inference.detection.dispatcher.logger") as log:
        dispatcher._log_pressure_snapshot()

    log.info.assert_called_once()
    # 渲染最终消息文本，校验含标记与 delta
    fmt, *args = log.info.call_args.args
    rendered = fmt % tuple(args)
    assert "[INFER_PRESSURE]" in rendered
    assert "drop=5(+5)" in rendered
