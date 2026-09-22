"""推理管线内部传输对象（online 热路径，内存流转，无序列化）。

- DetectionTask（入）：对某 client/stage 的某帧做检测，由 dispatcher 构造、按 stage 入队。
- FrameInference（出）：一帧多检测器聚合，detections[detector_name] = FrameDetections。

均为进程内 dataclass（非 wire DTO），不背 Pydantic 校验。跨服务共享契约来自 `app.domain`：
检测 `FrameDetections` / 特征 `FrameFeature`、时序事实 `EventFact` / `SegmentFact`（`app.domain.fact`）、
告警 `app.domain.alarm`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, TYPE_CHECKING

import numpy as np

from app.domain.detection import FrameDetections

if TYPE_CHECKING:
    from app.services.client import ClientQueues


# ==================== 传输对象（online 热路径）====================


@dataclass
class DetectionTask:
    """推理请求（队列作业）：对某 client/stage 的某帧做检测。

    cq 为 dispatcher 在 pop 帧时捕获的 per-run CQ 句柄，随 batch 透传到 FrameInference，
    供写回凭它投递而**不按键反查**（消除 dispatch→infer→write-back 期间换槽的跨 run 串台）。
    task_id 为运行键（路由标识），随句柄同行，仅日志/诊断用；路由靠 cq 句柄。
    """

    task_id: int
    stage: str
    timestamp: float
    frame: np.ndarray
    cq: "ClientQueues"


@dataclass
class FrameInference:
    """推理结果：一帧多检测器聚合（detections[detector_name] = FrameDetections）。

    timestamp 为帧捕获 ts。本对象是 pool→写回口的传输消息，不被 cq 留存（写回口把
    detections 物化成 FrameFeature 存入 slide_window/latest_inference，二者均无 cq）。
    cq 为从对应 DetectionTask 透传的捕获句柄，写回只写它、不反查；旧句柄经 CQ 状态机
    （DRAINING/CLOSED）被挡，碰不到新 run。
    frame_width/frame_height 为帧分辨率：fan-out 前定死的每帧常量，pool 从原始帧盖章、随本消息透传，
    写回口物化进 FrameFeature（原始帧此后即销毁，此处是唯一采集时机）。拆两字段避免 (w,h) 隐式序混淆。
    """

    task_id: int
    stage: str
    timestamp: float
    detections: Dict[str, FrameDetections]
    cq: "ClientQueues"
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
