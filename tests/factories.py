"""测试数据构造的单一真源：领域对象 + 运行态 CQ + 推理消息。

纯函数、**无 pytest 依赖** —— 可被 tests/ 直接 `from factories import ...`（pytest prepend
模式把 tests/ 目录插进 sys.path），也可被 integration_tests/ 复用。

设计约定：
- 每个 builder 带"最常见良性态"默认值，用例只写它关心的偏差（关键字 override）。
- 契约一变（如 CQ 构造签名、FrameInference 加字段），只改这一处，不再扫散点。
- MagicMock 化的 CQ / DB 会话属于"单文件专用替身"，不在此集中（集中无收益）。
"""

from typing import Dict, List, Optional

import numpy as np

from app.domain.alarm import Alarm
from app.domain.detection import Detection, FrameDetections
from app.domain.frame import Frame
from app.services.client.queues import ClientQueues
from app.services.inference.models import FrameInference

__all__ = [
    "make_detection",
    "make_frame_detections",
    "make_frame",
    "make_cq",
    "make_bare_cq",
    "make_frame_inference",
    "make_alarm",
]


def make_detection(
    *, bbox: Optional[List[int]] = None, confidence: float = 0.9,
    class_id: int = 0, class_name: str = "bubble", **over,
) -> Detection:
    """单个检测框。bbox 值对大多数断言无关紧要，默认 [0,0,1,1]。"""
    return Detection(
        bbox=list(bbox) if bbox is not None else [0, 0, 1, 1],
        confidence=confidence,
        class_id=class_id,
        class_name=class_name,
        **over,
    )


def make_frame_detections(
    *, n: int = 1, class_name: str = "bubble", ts: float = 1.0,
    metadata: Optional[Dict] = None, **over,
) -> FrameDetections:
    """一帧的多检测聚合（n 个同类检测）。n=0 表示该帧无检测。"""
    return FrameDetections(
        detections=[make_detection(class_name=class_name) for _ in range(n)],
        metadata=metadata if metadata is not None else {},
        timestamp=ts,
        **over,
    )


def make_frame(*, ts: float = 1.0, shape=(4, 4, 3)) -> Frame:
    """全零 numpy 帧（内容对断言无关，只用 timestamp）。"""
    return Frame(timestamp=ts, frame=np.zeros(shape, dtype=np.uint8))


def make_cq(
    *, task_id: int = 1, step_id: Optional[int] = 1,
    source_ip: str = "c1", stage: str = "1", **kw,
) -> ClientQueues:
    """带不可变运行身份的 CQ（一 CQ == 一 run）。透传 ca_maxlen 等队列参数。"""
    return ClientQueues(
        task_id=task_id, step_id=step_id, source_ip=source_ip, stage=stage, **kw
    )


def make_bare_cq(**kw) -> ClientQueues:
    """无身份裸建（算子/纯队列单测，stage 默认 MOCK）。"""
    return ClientQueues(**kw)


def make_frame_inference(
    *, cq: Optional[ClientQueues] = None, task_id: Optional[int] = None,
    stage: Optional[str] = None, ts: float = 1.0,
    detectors: Optional[Dict[str, FrameDetections]] = None,
) -> FrameInference:
    """推理结果消息。task_id/stage 缺省从 cq 派生（无 cq 时回退 1/"3"）。

    detectors 缺省为单流 {"bubble": <1 检测>}；写回句柄 fence 类测试传 cq=<句柄>，
    离线/直连 FeatureStore 类测试传 cq=None 并显式给 detectors。
    """
    if detectors is None:
        detectors = {"bubble": make_frame_detections(ts=ts)}
    return FrameInference(
        task_id=task_id if task_id is not None else (cq.task_id if cq is not None else 1),
        stage=stage if stage is not None else (cq.stage if cq is not None else "3"),
        timestamp=ts,
        detections=detectors,
        cq=cq,
    )


def make_alarm(
    *, alarm_type="流程违规", alarm_level: str = "high",
    alarm_message: str = "test alarm", metric="BUBBLE",
    mode: str = "REALTIME", stage: str = "LEAK", **over,
) -> Alarm:
    """告警记录。默认实时 BUBBLE；枚举型 alarm_type/metric 可按需 override。"""
    return Alarm(
        alarm_type=alarm_type,
        alarm_level=alarm_level,
        alarm_message=alarm_message,
        metric=metric,
        mode=mode,
        stage=stage,
        **over,
    )
