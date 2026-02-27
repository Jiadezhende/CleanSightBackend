"""推理服务标准数据模型（InferenceWorkflow 级别）

本模块定义 **单个 InferenceWorkflow** 的数据结构，用于：
- 标准化不同检测模型的输出格式
- 定义 Task 的推理、时序分析、可视化、告警评估的数据契约

数据模型层次：
- 本模块（data_models.py）: Task 级别 - 单个检测任务的数据结构
- models.py: 客户端/Stage 级别 - 汇总多个 Task 的结果，传递给下游队列

数据结构：
- Detection: 单个检测对象（bbox, confidence, class_name）
- DetectionOutput: 检测输出（标准化格式，包含 detections 列表、success 状态等）
- TemporalResult: Task 的时序分析结果
- VisualizationData: Task 的可视化数据
- AlarmInfo: Task 的告警信息

使用示例：
    # Task 级别：单个 BubbleDetectionTask 的输出
    task_result: DetectionOutput = DetectionOutput(
        detections=[...],
        metadata={},
        timestamp=time.time(),
        success=True,
        bubble_detected=True,
        bubble_count=5
    )
    
    # 客户端级别：InferenceResult 汇总多个 Task
    inference_result = InferenceResult(
        client_id="client_001",
        stage="LEAK",
        result={
            "bubble_detection": task_result1,  # DetectionOutput
            "bending_detection": task_result2   # DetectionOutput
        }
    )
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import numpy as np


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
class DetectionOutput:
    """检测输出（适配器统一输出）
    
    所有检测策略的输出经过适配器转换为此标准格式。
    此类同时作为推理结果的最终输出格式。
    """
    detections: List[Detection]          # 检测结果列表
    metadata: Dict[str, Any]             # 元数据（如模型名称、推理时间等）
    timestamp: float                     # 时间戳
    success: bool = True                 # 推理是否成功
    error: Optional[str] = None          # 错误信息（失败时提供）
    
    # 向后兼容字段（气泡检测）
    bubble_detected: Optional[bool] = None
    bubble_count: Optional[int] = None
    
    # 向后兼容字段（弯折检测）
    bending_detected: Optional[bool] = None
    detection_count: Optional[int] = None


@dataclass
class TemporalResult:
    """时序分析结果
    
    每个 InferenceWorkflow 的 analyze_temporal() 方法返回此结果
    """
    detected: bool                       # 当前帧是否检测到目标
    event_triggered: bool                # 是否触发时序事件（如连续3帧）
    event_message: Optional[str]         # 事件描述（如"连续3帧检测到气泡"）
    counters: Dict[str, Any] = field(default_factory=dict)  # 计数器（支持int/float，如 bubble_count、consecutive_frames、window_ratio）
    metadata: Dict[str, Any] = field(default_factory=dict)  # 其他元数据


@dataclass
class VisualizationData:
    """可视化数据
    
    每个 InferenceWorkflow 的 prepare_visualization_data() 方法返回此数据
    供固定渲染器使用
    """
    type: str                            # 可视化类型: "bbox", "mask", "heatmap", "keypoint"
    items: List['VisItem']               # 可视化项列表
    status_text: str                     # 状态栏文本
    status_color: Tuple[int, int, int]   # 状态栏颜色 (B, G, R)
    status_position: str = "top-right"   # 状态栏位置: "top-left", "top-right", "bottom-left", "bottom-right"


@dataclass
class VisItem:
    """单个可视化项
    
    根据 VisualizationData.type 的不同，需要提供不同的字段：
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


@dataclass
class AlarmInfo:
    """告警信息
    
    每个 InferenceWorkflow 的 evaluate_alarms() 方法返回告警列表
    """
    alarm_type: str                      # 告警类型（如"流程违规"）
    alarm_level: str                     # 告警级别: "low", "medium", "high", "critical"
    alarm_message: str                   # 告警消息
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据


# 可视化类型枚举（供参考）
class VisualizationType:
    """可视化类型常量"""
    BBOX = "bbox"              # 检测框
    MASK = "mask"              # 分割掩码
    HEATMAP = "heatmap"        # 热力图
    KEYPOINT = "keypoint"      # 关键点
