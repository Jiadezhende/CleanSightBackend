"""守卫：多 detector 聚合后，同帧各流 FrameDetections.timestamp 必须相等（= 帧捕获真值锚点）。

背景：detector 曾在推理时各自 time.time() 生成 FrameDetections.timestamp，违反 Operator._zip_by_ts
"同帧多流 ts 精确相等（来自同一 FrameInference.timestamp）"的对齐不变式——单流算子侥幸不受影响，
但多流融合算子会因两流 ts 对不上而 inner-join 交集为空、整帧被丢。

修复：帧捕获 ts 从 pool.infer_batch 一路穿到 detector，令每帧
FrameDetections.timestamp == FrameInference.timestamp == Frame.timestamp。本用例锁死该不变式。
"""

from typing import Dict, List, Tuple

import numpy as np

from app.domain.alarm import Alarm
from app.domain.detection import FrameDetections
from app.services.inference.detection.pool import MultiModelWorkerPool
from app.services.inference.models import DetectionTask
from app.services.inference.temporal.operator import Operator
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


class _TwoStreamOperator(Operator):
    """订阅两条流的最小算子，仅用来驱动 _zip_by_ts。"""

    def analyze(self, windows: Dict[str, List[FrameDetections]]) -> None:  # pragma: no cover
        pass

    def judge(self) -> Tuple[List[str], List[Alarm]]:  # pragma: no cover
        return [], []


def test_multi_detector_same_frame_shares_capture_ts():
    """同一帧经两个 detector 后，两流 FrameDetections.timestamp 都等于 req.timestamp。"""
    pool = MultiModelWorkerPool(
        stage="1",
        models=[_mock_detector("streamA"), _mock_detector("streamB")],
        use_cuda_stream=False,
    )
    cq = make_cq()
    ts = 123.456
    results = pool.infer_batch([_task(ts, cq)])

    assert len(results) == 1
    fi = results[0]
    assert fi.timestamp == ts
    assert set(fi.detections) == {"streamA", "streamB"}
    # 核心不变式：两流的 FrameDetections.timestamp 相等且等于帧捕获真值锚点
    for name, fd in fi.detections.items():
        assert fd.timestamp == ts, f"{name} 流 ts={fd.timestamp} != 锚点 {ts}"


def test_zip_by_ts_aligns_multi_stream_after_fix():
    """两流经 pool 后，_zip_by_ts inner-join 能对齐（不再因 ts 不等而漏帧）。"""
    pool = MultiModelWorkerPool(
        stage="1",
        models=[_mock_detector("streamA"), _mock_detector("streamB")],
        use_cuda_stream=False,
    )
    cq = make_cq()
    ts = 77.0
    fi = pool.infer_batch([_task(ts, cq)])[0]

    op = _TwoStreamOperator(name="fuse", subscribes=["streamA", "streamB"], window_seconds=10.0)
    windows = {name: [fd] for name, fd in fi.detections.items()}
    aligned = op._zip_by_ts(windows)

    assert len(aligned) == 1, "多流同帧应对齐出一帧；若为空说明 ts 不等的回归"
    assert aligned[0].ts == ts
    assert set(aligned[0].by_source) == {"streamA", "streamB"}
