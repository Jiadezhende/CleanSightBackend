"""测试替身：无权重、无 torch 的 Detector / OfflineSegmenter（生产配置与代码里没有 MOCK），
以及跨文件共用的 I/O 替身。

    MockDetector         纯 numpy 亮度启发式 Detector（中心区灰度均值 < 阈值即出框）
    BrushRulesSegmenter  纯规则离线分段（任一 source 有框即 active，连续 active 帧并段）
    FakeSpawner          离线作业服务的子进程启动器；FakeWorkerProcess 由用例手动 `finish()`
    wait_until           轮询直到条件成立，超时即断言失败

测试 config 里按模块名引用：`{"class": "doubles.BrushRulesSegmenter"}`（tests/ 在 sys.path 上）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Sequence

import numpy as np

from app.types.detection import DetBox, DetectorOutput, FrameDetection
from app.services.inference.online.render import RenderItem, RenderSpec, RenderType
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.offline.segmentation.segmenter import OfflineContext, OfflineSegmenter
from app.services.inference.offline.segmentation.utils import FrameInputContract
from app.services.inference.online.detection.detector import Detector

_MOCK_CLASS_ID = 0
_MOCK_CLASS_NAME = "mock_object"


class MockDetector(Detector):
    """Mock 检测器（纯 numpy，无模型）。无状态，多 Client 共享。

    取帧中心 1/4 区域灰度均值，均值 < brightness_threshold 视为检测到目标。
    """

    def __init__(
        self,
        brightness_threshold: float = 100.0,
        enabled: bool = True,
    ):
        super().__init__(name="mock", enabled=enabled)
        self.brightness_threshold = brightness_threshold

    def _detect(self, frame: np.ndarray, timestamp: float) -> DetectorOutput:
        """单帧亮度启发式检测。timestamp 为帧捕获真值锚点，由 infer_batch 穿入。"""
        h, w = frame.shape[:2]
        cy1, cy2 = h // 4, 3 * h // 4
        cx1, cx2 = w // 4, 3 * w // 4
        center_crop = frame[cy1:cy2, cx1:cx2]

        gray = np.mean(center_crop, axis=2) if center_crop.ndim == 3 else center_crop.astype(float)
        mean_brightness = float(np.mean(gray))

        detections: List[DetBox] = []
        if mean_brightness < self.brightness_threshold:
            confidence = 1.0 - mean_brightness / 255.0
            detections.append(DetBox(
                bbox=[cx1, cy1, cx2, cy2],
                confidence=round(confidence, 4),
                class_id=_MOCK_CLASS_ID,
                class_name=_MOCK_CLASS_NAME,
                extra={"mean_brightness": round(mean_brightness, 2)},
            ))

        return DetectorOutput(
            boxes=detections,
            metadata={
                "model": "mock_brightness",
                "mean_brightness": round(mean_brightness, 2),
            },
            timestamp=timestamp,
            success=True,
        )

    def infer_batch(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[DetectorOutput]:
        return [self._detect(frame, ts) for frame, ts in zip(frames, timestamps)]

    def prepare_visualization_data(self, output: DetectorOutput) -> RenderSpec:
        items = [
            RenderItem(
                bbox=det.bbox,
                label=f"[MOCK] {det.confidence:.2f}",
                confidence=det.confidence,
                color=(255, 128, 0),
            )
            for det in output.boxes
        ]

        detected = len(output.boxes) > 0
        brightness = output.metadata.get("mean_brightness", "-")

        if detected:
            status_text = f"[MOCK] Detected (lum={brightness})"
            status_color = (0, 128, 255)
        else:
            status_text = f"[MOCK] Clear (lum={brightness})"
            status_color = (0, 200, 0)

        return RenderSpec(
            type=RenderType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )


class BrushRulesSegmenter(OfflineSegmenter[Sequence[FrameDetection]]):
    """纯规则 Mock 分段器。

    Args:
        label: active 片段写出的动作标签，默认 `mock_action`。
    """

    frame_contract = FrameInputContract()

    def __init__(self, label: str = "mock_action"):
        self.label = label

    def _build_model_input(self, frames: Sequence[FrameDetection], context: OfflineContext) -> Sequence[FrameDetection]:
        """Mock 不做特征工程，直接把帧序列交给规则逻辑。"""
        return frames

    def _predict(self, model_input: Sequence[FrameDetection]) -> LabelProbs:
        active = np.array([any(out.boxes for out in frame.by_source.values()) for frame in model_input], dtype=bool)
        return LabelProbs(
            ts=np.array([frame.ts for frame in model_input], dtype=np.float64),
            probs=np.stack((~active, active), axis=1).astype(np.float32),
            labels=("idle", self.label),
        )

    def _postprocess(self, label_probs: LabelProbs) -> List[TemporalSegment]:
        segments: List[TemporalSegment] = []
        run_start: float | None = None
        run_last = 0.0
        for ts, active in zip(label_probs.ts.tolist(), label_probs.probs[:, 1]):
            if active:
                if run_start is None:
                    run_start = ts
                run_last = ts
                continue
            if run_start is not None:
                segments.append(self._make(run_start, run_last))
            run_start = None

        if run_start is not None:
            segments.append(self._make(run_start, run_last))
        return segments

    def _make(self, start: float, end: float) -> TemporalSegment:
        return TemporalSegment(
            producer=self.name,
            label=self.label,
            start=float(start),
            end=float(end),
            conf=1.0,
            meta={"model_version": "brush_rules_v1"},
        )


# ---------------------------------------------------------------------------
# 离线作业子进程
# ---------------------------------------------------------------------------


class FakeWorkerProcess:
    """离线作业子进程替身：用例手动 `finish()` / `kill()`，不起真进程、不碰 torch。

    `send_result()` 只置结果、不置退出，用来复现「结果先到、进程后退出」的真实次序。
    """

    def __init__(self, spawn_args):
        self.args = spawn_args  # spawner 收到的 (task_id, step_id, run_id, threads)
        self.pid = 4242
        self.exitcode = None
        self.killed = False
        self._result = None
        self._at_eof = False
        self._done = threading.Event()
        self._closed = False

    def send_result(self, result):
        self._result = result

    def finish(self, exitcode=0, result=None):
        if result is not None:
            self._result = result
        self.exitcode = exitcode
        self._at_eof = True  # 子进程退出即关闭写端 / 管道 EOF
        self._done.set()

    def is_alive(self):
        return not self._done.is_set()

    def join(self, timeout=None):
        self._done.wait(timeout)

    def poll_result(self, timeout):
        if self._result is not None:
            return self._result
        self._done.wait(timeout)
        return self._result

    @property
    def at_eof(self):
        return self._at_eof

    def kill(self):
        self.killed = True
        self.finish(exitcode=-9)

    def close(self):
        self._closed = True


class FakeSpawner:
    def __init__(self):
        self.procs: List[FakeWorkerProcess] = []

    def __call__(self, task_id, step_id, run_id, threads):
        proc = FakeWorkerProcess((task_id, step_id, run_id, threads))
        self.procs.append(proc)
        return proc


def offline_result(status="completed", producer="P", segment_count=3, message="", alarm_count=None):
    """子进程结果通道 / CLI 末行 JSON 的 payload。"""
    return {"status": status, "producer": producer, "segment_count": segment_count, "alarm_count": alarm_count,
            "message": message}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("条件未在时限内满足")
