"""测试数据构造的单一真源：领域对象 + 运行态 CQ + 推理消息。

纯函数、**无 pytest 依赖** —— 可被 tests/ 直接 `from factories import ...`（pytest prepend
模式把 tests/ 目录插进 sys.path），也可被 integration_tests/ 复用。

设计约定：
- 每个 builder 带"最常见良性态"默认值，用例只写它关心的偏差（关键字 override）。
- 契约一变（如 CQ 构造签名、FrameDetection 加字段），只改这一处，不再扫散点。
- MagicMock 化的 CQ / DB 会话属于"单文件专用替身"，不在此集中（集中无收益）。
"""

from typing import Dict, List, Optional

import numpy as np

from app.domain.alarm import Alarm
from app.domain.detection import DetBox, DetectorOutput, FrameDetection
from app.domain.frame import Frame
from app.services.client.queues import ClientQueues

__all__ = [
    "make_det_box",
    "make_detector_output",
    "make_frame_detection",
    "make_frame",
    "make_cq",
    "make_bare_cq",
    "make_alarm",
    "seed_hls_segments",
]


def make_det_box(
    *, bbox: Optional[List[int]] = None, confidence: float = 0.9,
    class_id: int = 0, class_name: str = "bubble", **over,
) -> DetBox:
    """单个检测框。bbox 值对大多数断言无关紧要，默认 [0,0,1,1]。"""
    return DetBox(
        bbox=list(bbox) if bbox is not None else [0, 0, 1, 1],
        confidence=confidence,
        class_id=class_id,
        class_name=class_name,
        **over,
    )


def make_detector_output(
    *, n: int = 1, class_name: str = "bubble", ts: float = 1.0,
    metadata: Optional[Dict] = None, **over,
) -> DetectorOutput:
    """一个检测器一帧的输出（n 个同类框）。n=0 表示该帧无检测。"""
    return DetectorOutput(
        boxes=[make_det_box(class_name=class_name) for _ in range(n)],
        metadata=metadata if metadata is not None else {},
        timestamp=ts,
        **over,
    )


def make_frame_detection(
    *, ts: float = 1.0, by_source: Optional[Dict[str, DetectorOutput]] = None,
    source: str = "bubble", n: int = 1, class_name: str = "bubble",
    metadata: Optional[Dict] = None,
    frame_width: Optional[int] = None, frame_height: Optional[int] = None,
    cq: Optional[ClientQueues] = None,
) -> FrameDetection:
    """一帧多流对齐的检测结果。by_source 缺省单流 {source: <n 个框>}。

    frame_width/frame_height 为帧级分辨率，缺省 None（消费方走默认兜底）。
    cq 缺省 None（留存态）；写回句柄 fence 类测试传 cq=<句柄> 模拟 collector 刚组装的帧。
    """
    if by_source is None:
        by_source = {
            source: make_detector_output(n=n, class_name=class_name, ts=ts, metadata=metadata)
        }
    return FrameDetection(
        ts=ts, by_source=by_source, frame_width=frame_width, frame_height=frame_height, cq=cq,
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
    """无身份裸建（算子/纯队列单测，task_id/step_id/stage 均为空默认值）。"""
    return ClientQueues(**kw)


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


def seed_hls_segments(
    task_id: int,
    step_id: int,
    items,
    *,
    track: str = "raw",
    with_init: bool = True,
    default_extinf_s: float = 10.0,
):
    """在 `{task}/{step}/hls/` 铺段文件 + init，**并登记进清单**；返回域目录。

    `items` 收 `[ts_us]` 或 `[(ts_us, extinf_s)]`。

    **登记那一步不能省**：「有哪些段」只由清单回答，光有段文件 = 没有段（在途，或
    `_m3u8.append` 失败留下的孤儿）。不走 `hls.insert_segment` 只是为了免拉 cv2/ffmpeg，
    落盘形态与它一致。

    调用前须让 `settings.storage_dir` 指到临时目录（conftest 的 `tmp_storage` fixture）。
    """
    from app.storage import hls
    from app.storage.hls import _m3u8

    normalised = [it if isinstance(it, tuple) else (it, default_extinf_s) for it in items]
    domain_dir = hls.init_path(task_id, step_id, track).parent
    domain_dir.mkdir(parents=True, exist_ok=True)

    for ts_us, extinf_s in normalised:
        ref = hls.SegmentRef(track=track, ts_us=ts_us)
        path = hls.segment_path(task_id, step_id, ref, create=True)
        path.write_bytes(b"fake-fmp4")
        _m3u8.append(
            hls.playlist_path(task_id, step_id, track),
            hls.init_name(track), extinf_s, path.name,
        )
    if with_init:
        hls.init_path(task_id, step_id, track).write_bytes(b"fake-init")
    return domain_dir
