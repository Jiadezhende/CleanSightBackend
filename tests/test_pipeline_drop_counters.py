"""推理链路静默丢帧计数器测试。

覆盖 StageAwareDispatcher 的计数点与压力行：
- _stage_queues 满（maxlen）时静默淘汰 → get_stage_drops()
- proxy 拒收（submit 返 False）→ _stage_rejects，并入周期压力行

ClientQueues 各 CA 队列的丢帧计数与压力行见 test_cq_pressure_log。
"""

import logging
import time
from unittest.mock import MagicMock

from factories import make_bare_cq, make_frame
from app.services.inference.online.detection.dispatcher import StageAwareDispatcher
from app.services.utils.pressure import PRESSURE_LOGGER_NAME


def _frame():
    return make_frame(ts=0.0, shape=(2, 2, 3))


def test_stage_queue_drop_counted_when_full():
    """_stage_queues 已满时再 dispatch 一帧，应记一次 stage 丢帧。"""
    cm = MagicMock()
    dispatcher = StageAwareDispatcher(client_service_instance=cm)

    # 客户端：ca_ready 有一帧，stage 为 ClientQueues 裸建默认值
    cq = make_bare_cq(ca_maxlen=10)
    cq.ca_ready.append(_frame())
    cm.snapshot.return_value = {"c1": cq}

    # 预填满该 stage 队列到 maxlen
    stage = cq.stage
    q = dispatcher._stage_queues[stage]
    for _ in range(q.maxlen):
        q.append(MagicMock())

    dispatcher._fetch_and_dispatch_round()

    assert dispatcher.get_stage_drops()[stage] == 1


def test_stage_queue_no_drop_when_not_full():
    """队列未满时 dispatch 不应记丢帧。"""
    cm = MagicMock()
    dispatcher = StageAwareDispatcher(client_service_instance=cm)

    cq = make_bare_cq(ca_maxlen=10)
    cq.ca_ready.append(_frame())
    cm.snapshot.return_value = {"c1": cq}

    dispatcher._fetch_and_dispatch_round()

    assert dispatcher.get_stage_drops().get(cq.stage, 0) == 0


def test_pressure_snapshot_silent_when_calm(caplog):
    """平稳（无丢帧、队列浅）时不应打印 [PRESSURE]，避免刷屏。"""
    cm = MagicMock()
    cm.snapshot.return_value = {}
    dispatcher = StageAwareDispatcher(client_service_instance=cm)
    # 放一条浅队列：空 _stage_queues 时采样循环不执行，静默是恒真的
    dispatcher._stage_queues["CLEAN"].append(make_frame(ts=time.time(), shape=(2, 2, 3)))

    with caplog.at_level(logging.INFO, logger=PRESSURE_LOGGER_NAME):
        dispatcher._log_pressure_snapshot()
        dispatcher._log_pressure_snapshot()

    assert "[PRESSURE]" not in caplog.text


def test_pressure_snapshot_logs_on_drop(caplog):
    """新增丢帧（delta>0）应打出一行 —— 即便队列此刻是浅的。

    丢完就空，水位天然测不到；"还在丢"必须自己成为压力信号。
    首次采样只播种基线（不把历史累计当成刚发生的），第二次才见涨。
    """
    cm = MagicMock()
    cm.snapshot.return_value = {}
    dispatcher = StageAwareDispatcher(client_service_instance=cm)
    dispatcher._stage_queues["CLEAN"]  # 触发 defaultdict 建 deque
    dispatcher._stage_drops["CLEAN"] = 0

    with caplog.at_level(logging.INFO, logger=PRESSURE_LOGGER_NAME):
        dispatcher._log_pressure_snapshot()          # 播种基线
        assert "[PRESSURE]" not in caplog.text
        dispatcher._stage_drops["CLEAN"] = 5         # 自上次报告以来新增丢帧
        dispatcher._log_pressure_snapshot()

    assert "component=dispatcher resource=stage_queue stage=CLEAN" in caplog.text
    assert "drop_total=5 drop_delta=5" in caplog.text


def test_submit_rejection_counted_into_pressure_line(caplog):
    """proxy 拒收（submit 返 False）不再是静默布尔——提交侧计数并进周期压力行。"""
    cq = make_bare_cq(ca_maxlen=10)
    cq.ca_ready.append(make_frame(ts=time.time(), shape=(2, 2, 3)))
    cm = MagicMock()
    cm.snapshot.return_value = {"c1": cq}
    stage = cq.stage
    dispatcher = StageAwareDispatcher(
        client_service_instance=cm,
        active_stages=[stage],
        submit_batch=lambda batch: False,      # 冒充 proxy 在途满
    )

    dispatcher._fetch_and_dispatch_round()
    dispatcher._drain_and_submit()

    assert dispatcher._stage_rejects[stage] == 1
    assert len(dispatcher._stage_queues[stage]) == 1   # 被拒不丢帧，帧原封留 deque

    with caplog.at_level(logging.INFO, logger=PRESSURE_LOGGER_NAME):
        dispatcher._log_pressure_snapshot()            # 播种基线
        dispatcher._drain_and_submit()                 # 再撞一次
        dispatcher._log_pressure_snapshot()

    assert "reject_total=2 reject_delta=1" in caplog.text


def test_pressure_snapshot_logs_on_high_watermark(caplog):
    """stage deque 越水位即打一行（无需丢帧/拒收）。"""
    cm = MagicMock()
    cm.snapshot.return_value = {}
    dispatcher = StageAwareDispatcher(client_service_instance=cm)

    q = dispatcher._stage_queues["CLEAN"]
    for _ in range(q.maxlen):
        q.append(make_frame(ts=time.time(), shape=(2, 2, 3)))

    with caplog.at_level(logging.INFO, logger=PRESSURE_LOGGER_NAME):
        dispatcher._log_pressure_snapshot()

    assert "component=dispatcher resource=stage_queue stage=CLEAN" in caplog.text
