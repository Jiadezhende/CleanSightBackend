"""守卫：多 detector 聚合后，同帧各流 DetectorOutput.timestamp 必须相等（= 帧捕获真值锚点）。

帧窗算子用 FrameDetection.ts 裁窗、用投影出的 DetectorOutput.timestamp 推进游标，两者必须
同源同值。帧捕获 ts 经 `StageWorker._infer_models` 穿到每个 detector，本用例锁死
DetectorOutput.timestamp == 入参 ts。
"""

import numpy as np

from app.services.inference.online.detection.stage_worker import StageWorker
from doubles import MockDetector


def _mock_detector(name: str) -> MockDetector:
    d = MockDetector(brightness_threshold=300.0)  # 阈值拉高→必命中，产出非空检测
    d.name = name  # 两个 mock 用不同流名，避免 merged 字典撞 key
    return d


def test_multi_detector_outputs_carry_capture_ts():
    worker = StageWorker(
        stage="1",
        models=[_mock_detector("streamA"), _mock_detector("streamB")],
    )
    frames = [np.zeros((8, 8, 3), dtype=np.uint8) for _ in range(2)]
    ts = [123.456, 123.5]

    merged = worker._infer_models(frames, ts)

    assert len(merged) == len(ts)
    for per_frame, want in zip(merged, ts):
        assert set(per_frame) == {"streamA", "streamB"}
        for name, out in per_frame.items():
            assert out.timestamp == want, f"{name} 流 ts={out.timestamp} != 锚点 {want}"
