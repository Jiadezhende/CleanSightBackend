"""写回句柄化 —— write-back 只写捕获的 frame.sess，不按 task_id 反查。

守两条不变式：
1. 迟到结果握旧 session 句柄 → 旧 session 非 ACTIVE（DRAINING/CLOSED）→ 三写（DETECTION_WINDOW /
   LATEST_DETECTION / DETECTIONS 落盘缓冲）全被挡，落 stale_run 计数，碰不到别的 run；
2. 同一 batch 内，stale run 被挡不殃及同批 ACTIVE run 的正常写回（跨 run 隔离）；
3. 推理降级帧（任一 source success=False）照常进帧窗 / 快照，但不进落盘缓冲。

写回口**不碰盘**：第三写只是入 DETECTIONS，真正落盘由 recording 的 sweeper 拉走，故这里
断言的是缓冲内容而不是文件。
"""

import pytest
from factories import make_session, make_detector_output, make_frame_detection
from app.services.session.core import Session
from app.services.inference.online.detection.service import DetectionService
from app.services.inference.online.topics import DETECTION_WINDOW, LATEST_DETECTION
from app.services.session.topics import DETECTIONS
from app.services.utils.metrics import frame_drop_total


def _result(cq: Session, ts: float = 1.0):
    return make_frame_detection(sess=cq, ts=ts)


def _bare_service() -> DetectionService:
    """绕过 __init__（避免加载模型）：write-back 只用 frame.sess，不读 self 的任何字段。"""
    return DetectionService.__new__(DetectionService)


def _stale_drops() -> float:
    return frame_drop_total.labels(reason="stale_run")._value.get()


def test_active_run_write_back_lands():
    svc = _bare_service()
    cq = make_session(task_id=1, step_id=3, source_ip="ipA", stage="3")

    res = _result(cq)
    svc._write_back_results([res])

    assert cq.channel(LATEST_DETECTION).get() is res  # 快照 = collector 组装的同一份 FrameDetection
    assert cq.channel(DETECTION_WINDOW).snapshot() == [res]  # 检测入滑窗
    # 落盘缓冲拿到同一份帧级 FrameDetection（不复制）
    buffered = cq.channel(DETECTIONS).drain()
    assert buffered == [res] and buffered[0] is res
    # 留存的帧不带写回句柄：写回口取走后置 None
    assert res.sess is None


@pytest.mark.parametrize("seal", [Session.seal, Session.close], ids=["draining", "closed"])
def test_non_active_run_write_back_blocked_and_counted(seal):
    svc = _bare_service()
    cq = make_session(task_id=2, step_id=3, source_ip="ipB", stage="3")
    seal(cq)  # 拆除封闸（DRAINING）/ 已拆除释放 payload（CLOSED）

    before = _stale_drops()
    svc._write_back_results([_result(cq)])

    assert cq.channel(LATEST_DETECTION).get() is None  # 快照未落
    assert cq.channel(DETECTION_WINDOW).snapshot() == []  # 滑窗未落
    assert cq.channel(DETECTIONS).drain() == []  # 落盘缓冲这条腿也被挡
    assert _stale_drops() - before == 1.0


def test_stale_and_active_in_same_batch_isolated():
    """同 batch 混跑：stale run 被挡，ACTIVE run 照常写回（不互殃）。"""
    svc = _bare_service()
    cq_stale = make_session(task_id=10, step_id=3, source_ip="ipS", stage="3")
    cq_active = make_session(task_id=11, step_id=3, source_ip="ipA", stage="3")
    cq_stale.seal()

    res_stale = _result(cq_stale)
    res_active = _result(cq_active)
    svc._write_back_results([res_stale, res_active])

    assert cq_stale.channel(LATEST_DETECTION).get() is None
    assert cq_active.channel(LATEST_DETECTION).get() is res_active
    assert cq_stale.channel(DETECTIONS).drain() == []  # 仅 active 进缓冲
    active_buffered = cq_active.channel(DETECTIONS).drain()
    assert len(active_buffered) == 1
    assert active_buffered[0] is res_active


def test_degraded_frame_not_buffered_for_persistence():
    """任一 source success=False 的降级帧：帧窗 / 快照照写（画面照常），落盘缓冲不收。"""
    svc = _bare_service()
    cq = make_session(task_id=20, step_id=3, source_ip="ipD", stage="3")
    degraded = make_frame_detection(sess=cq, ts=1.0, by_source={
        "clean_large": make_detector_output(n=1, ts=1.0),
        "clean_small": make_detector_output(n=0, ts=1.0, success=False),
    })
    ok = make_frame_detection(sess=cq, ts=2.0)

    svc._write_back_results([degraded, ok])

    assert cq.channel(DETECTION_WINDOW).snapshot() == [degraded, ok]
    assert cq.channel(LATEST_DETECTION).get() is ok
    assert cq.channel(DETECTIONS).drain() == [ok]


def test_first_inference_logged_once_per_run(caplog):
    """启动埋点 B：本 run 首个推理写回只打一次（写回线程是 LATEST_DETECTION 唯一写方）。"""
    import logging

    svc = _bare_service()
    cq = make_session(task_id=30, step_id=3, source_ip="ipF", stage="3")
    with caplog.at_level(logging.INFO, logger="app.startup_latency"):
        svc._write_back_results([_result(cq, ts=1.0)])
        svc._write_back_results([_result(cq, ts=2.0)])

    lines = [r.getMessage() for r in caplog.records if "first_inference" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("[startup] task=30 step=3 first_inference +")
