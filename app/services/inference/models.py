"""推理管线内部数据结构（传输对象 + 离线预留事实契约）。

两段，性质不同但同属"inference 私有数据形状"，故统一收此：

1. 传输对象（online 热路径，内存流转，无序列化）：
   - DetectionTask（入）：对某 client/stage 的某帧做检测，由 dispatcher 构造、按 stage 入队。
   - FrameInference（出）：一帧多检测器聚合，detections[detector_name] = FrameDetections。
2. 离线预留事实契约（见下方分节）：online 不产不消费。

均为进程内 dataclass（非 wire DTO），不背 Pydantic 校验。跨服务共享契约（FrameDetections）
来自 app.domain；告警契约见 app.domain.alarm。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, TYPE_CHECKING

import numpy as np

from app.domain.detection import FrameDetections

if TYPE_CHECKING:
    from app.services.client import ClientQueues


# ==================== 传输对象（online 热路径）====================


@dataclass
class DetectionTask:
    """推理请求（队列作业）：对某 client/stage 的某帧做检测。

    cq 为 dispatcher 在 pop 帧时捕获的 per-run CQ 句柄，随 batch 透传到 FrameInference，
    供写回凭它投递而**不按 client_id 反查**（消除 dispatch→infer→write-back 期间换槽的跨 run 串台）。
    client_id 为被动/诊断字段（值 = 运行键 task_id），仅日志用；路由靠 cq 句柄。
    """

    client_id: int
    stage: str
    timestamp: float
    frame: np.ndarray
    cq: "ClientQueues"


@dataclass
class FrameInference:
    """推理结果：一帧多检测器聚合（detections[detector_name] = FrameDetections）。

    timestamp 为帧捕获 ts，供 VisualizationWorker 按帧去重（同帧只渲染一次）。
    cq 为从对应 DetectionTask 透传的捕获句柄，写回只写它、不反查；旧句柄经 CQ 状态机
    （DRAINING/CLOSED）被挡，碰不到新 run。注：set_latest_inference 把本对象存进 cq 后形成
    cq→_latest_inference→cq 自引用环，benign（GC 处理循环，close() 释放 payload 时断开）。
    """

    client_id: int
    stage: str
    timestamp: float
    detections: Dict[str, FrameDetections]
    cq: "ClientQueues"


# ==================== 离线预留事实契约（L3 时序分析层产出）====================
#
# online 链路不产不消费事实（状态共享于 Operator._sm）。本段为离线 segmenter 预置，
# 由 FactLedger（store.py）落盘/回读。两类事实分开建模：
# - EventFact（打点）：实时滑窗产出的瞬时事实 = 某信号在 ts 的当前电平
# - SegmentFact（分段）：离线全序列产出的动作分割结果，timeline = List[SegmentFact]
# 二者均带 to_json/from_json，落 FactLedger（JSONL，带 type 判别字段）。
# 阈值/required 不进 fact.meta —— 那些归 Judge 持有。

_FACT_EVENT = "event"
_FACT_SEGMENT = "segment"


@dataclass
class EventFact:
    """打点：实时滑窗产出的瞬时事实 = 某信号在 ts 的当前电平。

    signal 是信号名（多信号靠不同名字区分，不是类型枚举判别字段）；
    同一 Analyzer 一个 tick 可产出多条 EventFact（不同 signal）。
    """

    source: str  # 来源检测点，如 "bubble"/"bending"
    signal: str  # 信号名，如 "birth_rate"/"state"/"count"
    value: Any  # 该信号在 ts 的当前值
    ts: float = field(default_factory=time.time)
    conf: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)  # 仅放伴随观测量，不放阈值/required

    def to_json(self) -> Dict[str, Any]:
        return {
            "type": _FACT_EVENT,
            "source": self.source,
            "signal": self.signal,
            "value": self.value,
            "ts": self.ts,
            "conf": self.conf,
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "EventFact":
        return cls(
            source=d["source"],
            signal=d["signal"],
            value=d["value"],
            ts=d.get("ts", 0.0),
            conf=d.get("conf", 1.0),
            meta=d.get("meta") or {},
        )


@dataclass
class SegmentFact:
    """分段：离线全序列产出的动作分割结果（timeline 的一个元素）。"""

    source: str  # 来源检测点
    label: str  # 动作标签，如 long_brushing
    start: float  # 分段起始时间
    end: float  # 分段结束时间
    conf: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {
            "type": _FACT_SEGMENT,
            "source": self.source,
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "conf": self.conf,
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "SegmentFact":
        return cls(
            source=d["source"],
            label=d["label"],
            start=d["start"],
            end=d["end"],
            conf=d.get("conf", 1.0),
            meta=d.get("meta") or {},
        )


def fact_from_json(d: Dict[str, Any]):
    """从 ledger JSON 行还原 Fact，按 type 判别字段分派。"""
    t = d.get("type")
    if t == _FACT_EVENT:
        return EventFact.from_json(d)
    if t == _FACT_SEGMENT:
        return SegmentFact.from_json(d)
    raise ValueError(f"未知 fact type: {t!r}")
