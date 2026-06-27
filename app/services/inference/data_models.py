"""推理服务标准数据模型（InferenceWorkflow 级别）

本模块定义 **单个 InferenceWorkflow** 的数据结构，用于：
- 标准化不同检测模型的输出格式
- 定义 Task 的推理、可视化、告警评估的数据契约

数据模型层次：
- 本模块（data_models.py）: Task 级别 - 单个检测任务的数据结构
- models.py: 客户端/Stage 级别 - 汇总多个 Task 的结果，传递给下游队列

数据结构：
- Detection: 单个检测对象（bbox, confidence, class_name）
- DetectionOutput: 检测输出（标准化格式，包含 detections 列表、success 状态等）
- VisualizationData: Task 的可视化数据
- AlarmInfo: Task 的告警信息
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# ==================== 检测契约（L1 检测层产出）====================
#
# 检测器（Detector.infer）产出标准化检测结果，并经 prepare_visualization_data
# 转出渲染数据。两者同属感知层，是对下游（时序分析 / 可视化）的输入契约。
# - Detection / DetectionOutput：检测结果标准格式
# - RenderSpec / RenderItem / RenderType：固定渲染器的输入数据


@dataclass
class Detection:
    """单个检测结果（标准格式）
    
    所有检测模型的输出都应转换为此格式
    """
    bbox: List[int]                      # [x1, y1, x2, y2]
    confidence: float                    # 置信度 [0.0-1.0]
    class_id: int                        # 类别ID
    class_name: str                      # 类别名称
    mask: Optional[np.ndarray] = None    # 分割掩码（可选）
    keypoints: Optional[List] = None     # 关键点（可选）
    extra: Dict[str, Any] = field(default_factory=dict)  # 扩展数据


@dataclass
class FrameDetections:
    """检测输出（适配器统一输出）
    
    所有检测策略的输出经过适配器转换为此标准格式。
    此类同时作为推理结果的最终输出格式。
    """
    detections: List[Detection]          # 检测结果列表
    metadata: Dict[str, Any]             # 元数据（如模型名称、推理时间等）
    timestamp: float                     # 时间戳
    success: bool = True                 # 推理是否成功
    error: Optional[str] = None          # 错误信息（失败时提供）



@dataclass
class RenderSpec:
    """可视化数据
    
    每个 InferenceWorkflow 的 prepare_visualization_data() 方法返回此数据
    供固定渲染器使用
    """
    type: str                            # 可视化类型: "bbox", "mask", "heatmap", "keypoint"
    items: List['RenderItem']               # 可视化项列表
    status_text: str                     # 状态栏文本
    status_color: Tuple[int, int, int]   # 状态栏颜色 (B, G, R)
    status_position: str = "top-right"   # 状态栏位置: "top-left", "top-right", "bottom-left", "bottom-right"


@dataclass
class RenderItem:
    """单个可视化项
    
    根据 RenderSpec.type 的不同，需要提供不同的字段：
    - bbox 类型: 需要 bbox 字段
    - mask 类型: 需要 mask 字段
    - heatmap 类型: 需要 heatmap 字段
    - keypoint 类型: 需要 keypoints 字段
    """
    bbox: Optional[List[int]] = None     # 边界框 [x1, y1, x2, y2]
    mask: Optional[np.ndarray] = None    # 分割掩码
    heatmap: Optional[np.ndarray] = None # 热力图
    keypoints: Optional[List] = None     # 关键点列表
    label: str = ""                      # 标签文本
    confidence: float = 0.0              # 置信度
    color: Tuple[int, int, int] = (0, 255, 0)  # 颜色 (B, G, R)
    extra: Dict[str, Any] = field(default_factory=dict)  # 扩展数据


class RenderType:
    """可视化类型常量（RenderSpec.type 取值）"""
    BBOX = "bbox"              # 检测框
    MASK = "mask"              # 分割掩码
    HEATMAP = "heatmap"        # 热力图
    KEYPOINT = "keypoint"      # 关键点


# ==================== 告警契约（L4 规则层产出）====================
#
# 规则层（Judge.step / finalize）消费事实后产出告警。
# - AlarmType / AlarmMetric / ALARM_MODE_*：告警分类与模式常量
# - _TASK_METRIC_MAP / _STAGE_ALIAS_MAP：YAML 驱动的可读性映射（启动时灌入）
# - AlarmInfo：单次告警的数据载体


class AlarmType(str, Enum):
    """告警类型枚举 — value 为外部持久化使用的中文字符串"""
    PROCESS_VIOLATION = "流程违规"
    TASK_TIMEOUT = "任务超时"
    MOCK = "mock_alarm"                  # 仅测试用


class AlarmMetric(str, Enum):
    """告警指标枚举 — value 为前端展示与持久化使用的字符串"""
    BUBBLE = "BUBBLE"
    BENDING = "BENDING"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    UNKNOWN = "UNKNOWN"


# 告警模式常量
ALARM_MODE_REALTIME = "REALTIME"
ALARM_MODE_SETTLEMENT = "SETTLEMENT"

# YAML model name → AlarmMetric 映射，由 InferenceManager.start() 初始化
# 通过 get_task_metric_map() 访问，不要直接读取此变量
_TASK_METRIC_MAP: Dict[str, AlarmMetric] = {}


def _set_task_metric_map(mapping: Dict[str, AlarmMetric]) -> None:
    """由 InferenceManager.start() 调用一次，初始化映射。"""
    global _TASK_METRIC_MAP
    _TASK_METRIC_MAP.clear()
    _TASK_METRIC_MAP.update(mapping)


def get_task_metric_map() -> Dict[str, AlarmMetric]:
    """返回 task_name → AlarmMetric 映射（由 YAML model name 驱动）。

    若映射尚未初始化（如单元测试场景），自动从 YAML 懒加载一次。
    """
    if not _TASK_METRIC_MAP:
        from app.services.inference.stage_factory import StageFactory
        from app.services.inference.config import load_stage_config
        _set_task_metric_map(StageFactory(load_stage_config()).build_task_metric_map())
    return _TASK_METRIC_MAP


# stage 主键(step_id) → alias 映射，由 InferenceManager.start() 初始化
# alias 仅用于可读性出口（写告警 step_name + 可视化叠字）；功能性标识一律用主键
_STAGE_ALIAS_MAP: Dict[str, str] = {}


def _set_stage_alias_map(mapping: Dict[str, str]) -> None:
    """由 InferenceManager.start() 调用一次，初始化 stage→alias 映射。"""
    global _STAGE_ALIAS_MAP
    _STAGE_ALIAS_MAP.clear()
    _STAGE_ALIAS_MAP.update(mapping)


def get_stage_alias(stage_key: str) -> str:
    """返回 stage 主键对应的可读别名；未命中回退主键本身。

    若映射尚未初始化（如单元测试场景），自动从 YAML 懒加载一次。
    """
    if not _STAGE_ALIAS_MAP:
        from app.services.inference.stage_factory import StageFactory
        from app.services.inference.config import load_stage_config
        _set_stage_alias_map(StageFactory(load_stage_config()).build_stage_alias_map())
    return _STAGE_ALIAS_MAP.get(stage_key, stage_key)


@dataclass
class Alarm:
    """告警信息

    由 Judge.step()（实时上升沿）/ Judge.finalize()（结算）产出。
    metric 由产出方（Judge）显式填入——它本就知道自己代表哪个指标，
    下游持久化直接读 alarm.metric，不再靠文案反推。
    """
    alarm_type: AlarmType                # 告警类型
    alarm_level: str                     # 告警级别: "low", "medium", "high", "critical"
    alarm_message: str                   # 告警消息
    metric: AlarmMetric = AlarmMetric.UNKNOWN  # 路由指标，产出方显式填
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据


# ==================== 事实契约（L3 时序分析层产出）====================
#
# 时序分析层只产「事实」，规则层（Judge）消费事实出告警。两类事实分开建模：
# - EventFact（打点）：实时滑窗产出的瞬时事实 = 某信号在 ts 的当前电平
# - SegmentFact（分段）：离线全序列产出的动作分割结果，timeline = List[SegmentFact]
#
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
    source: str                          # 来源检测点，如 "bubble"/"bending"
    signal: str                          # 信号名，如 "birth_rate"/"state"/"count"
    value: Any                           # 该信号在 ts 的当前值
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
    source: str                          # 来源检测点
    label: str                           # 动作标签，如 long_brushing
    start: float                         # 分段起始时间
    end: float                           # 分段结束时间
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
