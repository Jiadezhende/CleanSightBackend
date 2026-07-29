"""T4: 写回句柄化 —— write-back 只写捕获的 res.cq，不按 client_id 反查。

守两条不变式：
1. 迟到结果握旧 CQ 句柄 → 旧 CQ 非 ACTIVE（DRAINING/CLOSED）→ 三写（slide_window /
   latest_inference / feature_store）全被挡，落 stale_run 计数，碰不到别的 run；
2. 同一 batch 内，stale run 被挡不殃及同批 ACTIVE run 的正常写回（跨 run 隔离）。
"""

from factories import make_cq, make_frame_inference
from app.services.client.queues import ClientQueues
from app.services.inference.detection.service import DetectionService
from app.utils.metrics import frame_drop_total


def _result(cq: ClientQueues, ts: float = 1.0):
    return make_frame_inference(cq=cq, ts=ts)


class _SpyFeatureStore:
    def __init__(self):
        self.appended = []

    def append(self, task_id, step_id, feature, owner=None):
        self.appended.append((task_id, step_id, feature))


def _bare_service(feature_store) -> DetectionService:
    """绕过 __init__（避免加载模型）：write-back 只用 self._feature_store + res.cq。"""
    svc = DetectionService.__new__(DetectionService)
    svc._feature_store = feature_store
    return svc


def _stale_drops() -> float:
    return frame_drop_total.labels(reason="stale_run")._value.get()


def test_active_run_write_back_lands():
    fs = _SpyFeatureStore()
    svc = _bare_service(fs)
    cq = make_cq(task_id=1, step_id=3, source_ip="ipA", stage="3")

    res = _result(cq)
    svc._write_back_results([res])

    assert cq.get_latest_inference().by_source is res.detections  # 快照 = 物化 FrameFeature
    assert cq.get_slide_window()  # 检测入滑窗
    # 特征落盘键 = (task_id, step_id)；落的是帧级 FrameFeature（by_source 即 res.detections）
    assert [a[:2] for a in fs.appended] == [(1, 3)]
    assert fs.appended[0][2].by_source is res.detections


def test_draining_run_write_back_blocked_and_counted():
    fs = _SpyFeatureStore()
    svc = _bare_service(fs)
    cq = make_cq(task_id=2, step_id=3, source_ip="ipB", stage="3")
    cq.to_draining()  # 拆除封闸

    before = _stale_drops()
    svc._write_back_results([_result(cq)])

    assert cq.get_latest_inference() is None  # 快照未落
    assert cq.get_slide_window() == []  # 滑窗未落
    assert fs.appended == []  # 特征这条外部腿也被挡
    assert _stale_drops() - before == 1.0


def test_closed_run_write_back_blocked():
    fs = _SpyFeatureStore()
    svc = _bare_service(fs)
    cq = make_cq(task_id=3, step_id=3, source_ip="ipC", stage="3")
    cq.close()  # CLOSED 释放 payload

    before = _stale_drops()
    svc._write_back_results([_result(cq)])

    assert cq.get_latest_inference() is None
    assert fs.appended == []
    assert _stale_drops() - before == 1.0


def test_stale_and_active_in_same_batch_isolated():
    """同 batch 混跑：stale run 被挡，ACTIVE run 照常写回（不互殃）。"""
    fs = _SpyFeatureStore()
    svc = _bare_service(fs)
    cq_stale = make_cq(task_id=10, step_id=3, source_ip="ipS", stage="3")
    cq_active = make_cq(task_id=11, step_id=3, source_ip="ipA", stage="3")
    cq_stale.to_draining()

    res_stale = _result(cq_stale)
    res_active = _result(cq_active)
    svc._write_back_results([res_stale, res_active])

    assert cq_stale.get_latest_inference() is None
    assert cq_active.get_latest_inference().by_source is res_active.detections
    assert [a[:2] for a in fs.appended] == [(11, 3)]  # 仅 active 落盘
    assert fs.appended[0][2].by_source is res_active.detections
