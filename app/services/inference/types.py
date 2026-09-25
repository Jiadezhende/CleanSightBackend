"""推理管线内部传输对象（online 热路径，内存流转，无序列化）。

- DetectionTask（入）：对某 client/stage 的某帧做检测，由 dispatcher 构造、按 stage 入队。
- 出参直接是 `app.domain.detection.FrameDetection`（带 cq 写回句柄），不另立传输类型。

均为进程内 dataclass（非 wire DTO），不背 Pydantic 校验。跨服务共享契约来自 `app.domain`：
检测 `DetectorOutput` / `FrameDetection`、时序事实 `TemporalEvent` / `TemporalSegment`（`app.domain.temporal`）、
告警 `app.domain.alarm`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.services.client import ClientQueues


# ==================== 传输对象（online 热路径）====================


@dataclass
class DetectionTask:
    """推理请求（队列作业）：对某 client/stage 的某帧做检测。

    cq 为 dispatcher 在 pop 帧时捕获的 per-run CQ 句柄，随 batch 透传到 FrameDetection.cq，
    供写回凭它投递而**不按键反查**（消除 dispatch→infer→write-back 期间换槽的跨 run 串台）。
    task_id 为运行键（路由标识），随句柄同行，仅日志/诊断用；路由靠 cq 句柄。
    """

    task_id: int
    stage: str
    timestamp: float
    frame: np.ndarray
    cq: "ClientQueues"
