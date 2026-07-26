"""RemoteInferProxy 单测：写回正确性 + 防泄漏边界 + 子进程失败清理（不 spawn 真子进程/GPU）。

策略：绕过 _spawn_child，注入假 req_q、手动置 child_ready，直接驱动 submit / _handle_response /
_handle_child_failure。测的是主进程侧的 req_id 关联、pending 有界、孤儿清理——与 GPU 无关。
"""

import queue

import numpy as np
import pytest

from app.domain.detection import FrameDetections
from app.services.inference.models import DetectionTask
from app.services.inference.detection.infer_proxy import RemoteInferProxy
from app.utils.metrics import frame_drop_total, infer_failure_total, infer_latency_ms

from factories import make_cq


def _task(cq, ts: float, *, w: int = 8, h: int = 4) -> DetectionTask:
    return DetectionTask(
        task_id=cq.task_id, stage=cq.stage, timestamp=ts,
        frame=np.zeros((h, w, 3), dtype=np.uint8), cq=cq,
    )


def _proxy(*, max_inflight: int = 8):
    """建一个不 spawn 的 proxy：假 req_q + 置就绪，write_back 收进列表。"""
    captured = []
    p = RemoteInferProxy(
        active_stages=["1"], write_back=captured.append, max_inflight=max_inflight,
    )
    p._req_q = queue.Queue()          # 假请求队列（有 put(timeout=)）
    p._child_ready.set()              # 冒充子进程已就绪
    return p, captured


def _drop(reason: str) -> float:
    return frame_drop_total.labels(reason=reason)._value.get()


def test_submit_stores_pending_and_ships_frames():
    p, _ = _proxy()
    cq = make_cq(task_id=1, stage="1")
    assert p.submit([_task(cq, 1.0), _task(cq, 2.0)]) is True

    assert p._inflight == 1
    assert len(p._pending) == 1
    (req_id, records), = p._pending.items()
    assert [r.timestamp for r in records] == [1.0, 2.0]
    assert records[0].cq is cq
    # 送进子进程的是 (req_id, stage, frames, timestamps)
    req_id2, stage, frames, ts = p._req_q.get_nowait()
    assert req_id2 == req_id and stage == "1"
    assert len(frames) == 2 and ts == [1.0, 2.0]


def test_collect_pops_pending_and_writes_back_assembled():
    p, captured = _proxy()
    cq = make_cq(task_id=7, step_id=3, stage="1")
    p.submit([_task(cq, 1.0, w=8, h=4), _task(cq, 2.0, w=8, h=4)])
    (req_id, _), = p._pending.items()

    # 子进程回：每帧一个 {detector: FrameDetections}（用轻量 dict 占位即可）
    from factories import make_frame_detections
    fd0 = make_frame_detections(n=1, ts=1.0)
    fd1 = make_frame_detections(n=2, ts=2.0)
    p._handle_response((req_id, [{"clean": fd0}, {"clean": fd1}]))

    # pending 清空、inflight 归零（pop 生效）
    assert p._pending == {} and p._inflight == 0
    # write_back 收到 2 条正确重组的 FrameInference（cq 贴回、ts/wh 对齐）
    assert len(captured) == 1
    frame_infs = captured[0]
    assert [fi.timestamp for fi in frame_infs] == [1.0, 2.0]
    assert all(fi.cq is cq for fi in frame_infs)
    assert all((fi.frame_width, fi.frame_height) == (8, 4) for fi in frame_infs)
    assert frame_infs[0].detections["clean"] is fd0
    assert frame_infs[1].detections["clean"] is fd1


def test_max_inflight_backpressure_rejects_and_counts():
    p, _ = _proxy(max_inflight=2)
    cq = make_cq(stage="1")
    assert p.submit([_task(cq, 1.0)]) is True
    assert p.submit([_task(cq, 2.0)]) is True
    # 第三批：在途已满 → 拒收
    assert p.submit([_task(cq, 3.0)]) is False
    assert p._inflight == 2 and len(p._pending) == 2


def test_submit_rejected_when_child_not_ready():
    p, _ = _proxy()
    p._child_ready.clear()
    cq = make_cq(stage="1")
    assert p.submit([_task(cq, 1.0)]) is False
    assert p._inflight == 0 and p._pending == {}


def test_leak_bound_partial_responses():
    """submit N 批、只回 M 批 → pending == N-M（无泄漏、无残留）。"""
    p, _ = _proxy(max_inflight=16)
    cq = make_cq(stage="1")
    for i in range(5):
        assert p.submit([_task(cq, float(i))]) is True
    assert p._inflight == 5

    req_ids = list(p._pending.keys())
    for req_id in req_ids[:3]:
        p._handle_response((req_id, [{}]))
    assert len(p._pending) == 2 and p._inflight == 2


def test_child_failure_clears_pending_and_counts_restart_drops():
    p, _ = _proxy(max_inflight=16)
    p._no_restart.set()  # 阻止真重启（本测只验清理）
    cq = make_cq(stage="1")
    p.submit([_task(cq, 1.0), _task(cq, 2.0)])  # 2 帧
    p.submit([_task(cq, 3.0)])                   # 1 帧 → 共 3 在途帧

    before = _drop("infer_child_restart")
    p._handle_child_failure(dead=True, wedged=False)

    assert p._pending == {} and p._inflight == 0
    assert _drop("infer_child_restart") - before == 3.0


def test_orphan_response_ignored():
    """未知 req_id（子进程重启后的旧响应）→ 不写回、不崩、不动计数。"""
    p, captured = _proxy()
    p._handle_response((999, [{}]))
    assert captured == [] and p._pending == {} and p._inflight == 0


def test_failure_metric_derived_from_framedetections():
    """失败埋点直接从 merged 里 success=False 的 FrameDetections 派生（metadata.error_type 作 label）。"""
    p, _ = _proxy()
    cq = make_cq(stage="1")
    p.submit([_task(cq, 1.0)])
    (req_id, _), = p._pending.items()

    fd_fail = FrameDetections(
        detections=[], metadata={"error_type": "RuntimeError", "error": "boom"},
        timestamp=1.0, success=False, error="boom",
    )
    before = infer_failure_total.labels(model="clean_large", error_type="RuntimeError")._value.get()
    p._handle_response((req_id, [{"clean_large": fd_fail}]))
    after = infer_failure_total.labels(model="clean_large", error_type="RuntimeError")._value.get()
    assert after - before == 1.0


def test_latency_metric_derived_from_framedetections_metadata():
    """成功埋点从 merged 里 FrameDetections.metadata["infer_ms"] 派生（每模型每批一次，除以帧数）。"""
    p, _ = _proxy()
    cq = make_cq(stage="1")
    p.submit([_task(cq, 1.0), _task(cq, 2.0)])  # 2 帧
    (req_id, _), = p._pending.items()

    fd0 = FrameDetections(detections=[], metadata={"model": "yolo", "infer_ms": 20.0}, timestamp=1.0)
    fd1 = FrameDetections(detections=[], metadata={"model": "yolo", "infer_ms": 20.0}, timestamp=2.0)
    before = infer_latency_ms.labels(model="clean_large")._sum.get()
    # 同模型两帧共享同一批 infer_ms=20；去重后只 observe 一次、值 = 20/2 = 10
    p._handle_response((req_id, [{"clean_large": fd0}, {"clean_large": fd1}]))
    assert infer_latency_ms.labels(model="clean_large")._sum.get() - before == 10.0
