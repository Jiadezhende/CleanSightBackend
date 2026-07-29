"""守卫：多 detector 聚合后，同帧各流 FrameDetections.timestamp 必须相等（= 帧捕获真值锚点）。

背景：detector 曾在推理时各自 time.time() 生成 FrameDetections.timestamp，破坏"同帧多流 ts 精确
相等（来自同一 FrameInference.timestamp）"的不变式。写回口按 `FrameFeature(ts=res.timestamp,
by_source=res.detections)` 物化整帧多流；帧窗算子用 FrameFeature.ts 裁窗、用投影出的
FrameDetections.timestamp 推进游标，两者必须同源同值，否则内部对齐错乱。

修复：帧捕获 ts 从 StageWorker.infer_batch 一路穿到 detector，令每帧
FrameDetections.timestamp == FrameInference.timestamp == Frame.timestamp。本用例锁死该不变式。
"""

import numpy as np

from app.domain.detection import FrameFeature
from app.services.inference.detection.stage_worker import StageWorker
from app.services.inference.models import DetectionTask
from app.services.inference.workflows.mock import MockDetector

from factories import make_cq


def _mock_detector(name: str) -> MockDetector:
    d = MockDetector(brightness_threshold=300.0)  # 阈值拉高→必命中，产出非空检测
    d.name = name  # 两个 mock 用不同流名，避免 merged 字典撞 key
    return d


def _task(ts: float, cq) -> DetectionTask:
    return DetectionTask(
        task_id=cq.task_id, stage=cq.stage, timestamp=ts,
        frame=np.zeros((8, 8, 3), dtype=np.uint8), cq=cq,
    )


def test_multi_detector_same_frame_shares_capture_ts():
    """同一帧经两个 detector 后，两流 FrameDetections.timestamp 都等于 req.timestamp。"""
    worker = StageWorker(
        stage="1",
        models=[_mock_detector("streamA"), _mock_detector("streamB")],
    )
    cq = make_cq()
    ts = 123.456
    results = worker.infer_batch([_task(ts, cq)])

    assert len(results) == 1
    fi = results[0]
    assert fi.timestamp == ts
    assert set(fi.detections) == {"streamA", "streamB"}
    # 核心不变式：两流的 FrameDetections.timestamp 相等且等于帧捕获真值锚点
    for name, fd in fi.detections.items():
        assert fd.timestamp == ts, f"{name} 流 ts={fd.timestamp} != 锚点 {ts}"


def test_frame_feature_carries_all_streams_at_capture_ts():
    """两流经 StageWorker 后，写回口物化的 FrameFeature 携两流、ts = 帧捕获锚点（取代旧 _zip_by_ts 对齐）。"""
    worker = StageWorker(
        stage="1",
        models=[_mock_detector("streamA"), _mock_detector("streamB")],
    )
    cq = make_cq()
    ts = 77.0
    fi = worker.infer_batch([_task(ts, cq)])[0]

    # StageWorker 从原始帧 (8,8,3) 盖章帧级分辨率 frame_width/height
    assert (fi.frame_width, fi.frame_height) == (8, 8)

    # 写回口构造：FrameFeature(ts, by_source, frame_width, frame_height)——多流天然对齐同帧。
    feat = FrameFeature(
        ts=fi.timestamp, by_source=fi.detections,
        frame_width=fi.frame_width, frame_height=fi.frame_height,
    )
    assert feat.ts == ts
    assert (feat.frame_width, feat.frame_height) == (8, 8)
    assert set(feat.by_source) == {"streamA", "streamB"}
    assert all(fd.timestamp == ts for fd in feat.by_source.values())
