"""推理任务基类

Task-Centric 架构：每个 InferenceWorkflow 负责完整的推理流程
- 检测 (infer)
- 时序分析 (analyze_temporal)
- 可视化数据准备 (prepare_visualization_data)
- 告警评估 (evaluate_alarms)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import numpy as np

from app.services.client.state import ClientState
from app.services.inference.data_models import (
    AlarmInfo,
    DetectionOutput,
    TemporalResult,
    VisualizationData,
)


class InferenceWorkflow(ABC):
    """推理任务基类

    新架构设计：
    1. Task 内部组装检测策略（DetectionStrategy）和输出适配器（OutputAdapter）
    2. 检测输出统一为 DetectionOutput 格式
    3. 时序分析逻辑下沉到每个 Task（analyze_temporal）
    4. 可视化数据由 Task 准备（prepare_visualization_data），渲染由固定渲染器完成
    5. 告警评估逻辑在 Task 内部（evaluate_alarms）

    子类只需实现 4 个核心方法：
    - infer(): 执行检测
    - analyze_temporal(): 时序分析
    - prepare_visualization_data(): 准备可视化数据
    - evaluate_alarms(): 评估告警（可选）
    """

    def __init__(self, name: str, enabled: bool = True):
        """
        Args:
            name: 任务名称（如 "bubble", "bending"）
            enabled: 是否启用此任务
        """
        self.name = name
        self.enabled = enabled

    # ====== 核心抽象方法 ======

    @abstractmethod
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        """执行单帧检测推理

        新架构：返回标准化的 DetectionOutput 对象（而非Dict）

        Args:
            frame: 输入图像
            context: 上下文信息（包含 task、client_id 等）

        Returns:
            DetectionOutput: 标准化检测输出
        """
        pass

    @abstractmethod
    def analyze_temporal(
        self,
        state: ClientState,
        output: DetectionOutput,
        timestamp: float
    ) -> TemporalResult:
        """时序分析

        每个 InferenceWorkflow 实现自己的时序逻辑（连续帧、滑动窗口、累计计数等）

        Args:
            state: 客户端状态（用于存储计数器、历史数据）
            output: 检测输出
            timestamp: 当前时间戳

        Returns:
            TemporalResult: 时序分析结果
        """
        pass

    @abstractmethod
    def prepare_visualization_data(
        self,
        output: DetectionOutput,
        temporal: TemporalResult
    ) -> VisualizationData:
        """准备可视化数据

        Task 提供可视化数据（检测框、标签、状态栏文本等），
        由固定渲染器负责绘制

        Args:
            output: 检测输出
            temporal: 时序分析结果

        Returns:
            VisualizationData: 可视化数据
        """
        pass

    def evaluate_alarms(
        self,
        temporal: TemporalResult,
        context: Dict[str, Any]
    ) -> List[AlarmInfo]:
        """评估告警条件

        基于时序分析结果，判断是否需要触发告警

        Args:
            temporal: 时序分析结果
            context: 上下文信息（包含 client_id、stage 等）

        Returns:
            AlarmInfo 列表（空列表表示无告警）
        """
        # 默认实现：不触发告警（子类可选覆盖）
        return []

    # ====== 批量推理支持 ======

    def infer_batch(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        """批量推理

        默认实现：逐帧调用 infer() 并包装为 DetectionOutput 格式
        子类可覆盖以利用模型的批量接口加速推理（如 YOLO 的 batch predict）

        Args:
            frames: 输入图像列表
            contexts: 上下文信息列表

        Returns:
            List[DetectionOutput]: 推理结果列表，包含检测输出和成功状态
        """
        results = []
        for frame, ctx in zip(frames, contexts):
            try:
                output = self.infer(frame, ctx)
                # output 已经是 DetectionOutput，直接设置 success
                output.success = True
                results.append(output)
            except Exception as e:
                # 创建失败的 DetectionOutput
                results.append(DetectionOutput(
                    detections=[],
                    metadata={"error": str(e)},
                    timestamp=time.time(),
                    success=False,
                    error=str(e)
                ))
        return results

    # ====== 辅助方法 ======

    def requires_context(self) -> List[str]:
        """声明依赖的上下文

        Returns:
            依赖的任务名称列表（空列表表示无依赖）
        """
        return []
