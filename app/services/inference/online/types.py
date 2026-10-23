"""推理管线内部传输对象（online 热路径，内存流转，无序列化）。

- DetectionTask（入）：对某 client/stage 的某帧做检测，由 dispatcher 构造、按 stage 入队。
- 出参直接是 `app.types.detection.FrameDetection`（带 sess 写回句柄），不另立传输类型。

均为进程内 dataclass（非 wire DTO），不背 Pydantic 校验。跨服务共享契约来自 `app.types`：
检测 `DetectorOutput` / `FrameDetection`、时序事实 `TemporalEvent` / `TemporalSegment`（`app.types.temporal`）、
告警 `app.types.alarm`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.services.session.core import Session


# ==================== 传输对象（online 热路径）====================


@dataclass
class DetectionTask:
    """推理请求（队列作业）：对某 client/stage 的某帧做检测。

    sess 为 dispatcher 在 pop 帧时捕获的该 run 的 Session，随 batch 透传到 FrameDetection.sess，
    供写回凭它投递而**不按键反查**（消除 dispatch→infer→write-back 期间换槽的跨 run 串台）。
    task_id 为运行键（路由标识），随句柄同行，仅日志/诊断用；路由靠 sess 句柄。
    """

    task_id: int
    stage: str
    timestamp: float
    frame: np.ndarray
    sess: "Session"
