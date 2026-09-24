"""T4: 写回句柄化 —— write-back 只写捕获的 res.cq，不按 client_id 反查。

守两条不变式：
1. 迟到结果握旧 CQ 句柄 → 旧 CQ 非 ACTIVE（DRAINING/CLOSED）→ 三写（slide_window /
   latest_inference / ca_features 落盘缓冲）全被挡，落 stale_run 计数，碰不到别的 run；
2. 同一 batch 内，stale run 被挡不殃及同批 ACTIVE run 的正常写回（跨 run 隔离）。

写回口**不碰盘**：第三写只是入 cq 缓冲，真正落盘由 recording 的 sweeper 拉走，故这里
断言的是缓冲内容而不是文件。
"""

from factories import make_cq, make_frame_inference
from app.services.client.queues import ClientQueues
from app.services.inference.detection.service import DetectionService
from app.utils.metrics import frame_drop_total


def _result(cq: ClientQueues, ts: float = 1.0):
    return make_frame_inference(cq=cq, ts=ts)


def _bare_service() -> DetectionService:
    """绕过 __init__（避免加载模型）：write-back 只用 res.cq，不读 self 的任何字段。"""
    return DetectionService.__new__(DetectionService)


def _stale_drops() -> float:
    return frame_drop_total.labels(reason="stale_run")._value.get()


def test_active_run_write_back_lands():
    svc = _bare_service()
    cq = make_cq(task_id=1, step_id=3, source_ip="ipA", stage="3")

    res = _result(cq)
    svc._write_back_results([res])

    assert cq.get_latest_inference().by_source is res.detections  # 快照 = 物化 FrameDetection
    assert cq.get_slide_window()  # 检测入滑窗
    # 落盘缓冲拿到同一份帧级 FrameDetection（by_source 即 res.detections，不复制）
    buffered = cq.drain_ca_features()
    assert len(buffered) == 1
    assert buffered[0].by_source is res.detections


def test_draining_run_write_back_blocked_and_counted():
    svc = _bare_service()
    cq = make_cq(task_id=2, step_id=3, source_ip="ipB", stage="3")
    cq.to_draining()  # 拆除封闸

    before = _stale_drops()
    svc._write_back_results([_result(cq)])

    assert cq.get_latest_inference() is None  # 快照未落
    assert cq.get_slide_window() == []  # 滑窗未落
    assert cq.drain_ca_features() == []  # 落盘缓冲这条腿也被挡
    assert _stale_drops() - before == 1.0


def test_closed_run_write_back_blocked():
    svc = _bare_service()
    cq = make_cq(task_id=3, step_id=3, source_ip="ipC", stage="3")
    cq.close()  # CLOSED 释放 payload

    before = _stale_drops()
    svc._write_back_results([_result(cq)])

    assert cq.get_latest_inference() is None
    assert cq.drain_ca_features() == []
    assert _stale_drops() - before == 1.0


def test_stale_and_active_in_same_batch_isolated():
    """同 batch 混跑：stale run 被挡，ACTIVE run 照常写回（不互殃）。"""
    svc = _bare_service()
    cq_stale = make_cq(task_id=10, step_id=3, source_ip="ipS", stage="3")
    cq_active = make_cq(task_id=11, step_id=3, source_ip="ipA", stage="3")
    cq_stale.to_draining()

    res_stale = _result(cq_stale)
    res_active = _result(cq_active)
    svc._write_back_results([res_stale, res_active])

    assert cq_stale.get_latest_inference() is None
    assert cq_active.get_latest_inference().by_source is res_active.detections
    assert cq_stale.drain_ca_features() == []  # 仅 active 进缓冲
    active_buffered = cq_active.drain_ca_features()
    assert len(active_buffered) == 1
    assert active_buffered[0].by_source is res_active.detections
