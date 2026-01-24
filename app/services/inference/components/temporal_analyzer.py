"""时序分析器：执行时序逻辑，更新 ClientState。

职责：
- 消费推理结果
- 执行复杂时序逻辑（连续帧检测、滑动窗口、累计计数等）
- 更新 ClientState（状态、计数器）
- 生成事件（如"连续3帧检测到气泡"）
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.services.client import ClientState
from app.services.inference.models import InferenceResult, TemporalAnalysisResult


class TemporalAnalyzer(ABC):
    """时序分析器抽象基类。"""

    @abstractmethod
    def analyze(
        self,
        state: ClientState,
        result: InferenceResult,
        current_timestamp: Optional[float] = None,
    ) -> TemporalAnalysisResult:
        """分析推理结果，更新时序状态。

        Args:
            state: 客户端状态
            result: 推理结果
            current_timestamp: 当前时间戳（可选，默认使用 result.timestamp）

        Returns:
            TemporalAnalysisResult: 时序分析结果
        """
        pass


class DefaultTemporalAnalyzer(TemporalAnalyzer):
    """默认时序分析器实现。

    支持三种模式：
    1. consecutive: 连续帧检测
    2. accumulated: 累计计数
    3. sliding_window: 滑动窗口（2秒时间窗口）
    """

    def __init__(self, config: Dict[str, Any]):
        """初始化时序分析器。

        Args:
            config: 配置字典，例如：
            {
                "LEAK": {
                    "bubble": {
                        "mode": "consecutive",  # 连续帧模式
                        "threshold": 3,          # 连续3帧
                    },
                    "bending": {
                        "mode": "sliding_window",  # 滑动窗口模式
                        "window_seconds": 2.0,      # 2秒窗口
                        "ratio": 0.7,                # 70%比例
                    },
                },
                "CLEAN": {
                    "quality": {
                        "mode": "accumulated",  # 累计计数模式
                        "threshold": 10,          # 累计10次
                    },
                },
            }
        """
        self.config = config

    def analyze(
        self,
        state: ClientState,
        result: InferenceResult,
        current_timestamp: Optional[float] = None,
    ) -> TemporalAnalysisResult:
        """执行时序分析。"""
        stage = result.stage
        stage_config = self.config.get(stage, {})

        # 使用提供的时间戳或推理结果的时间戳
        timestamp = current_timestamp or result.timestamp

        events: List[str] = []
        stage_changed = False
        new_stage: Optional[str] = None
        step_completed = False

        # 遍历所有子任务结果
        for subtask_name, subtask_res in result.result.items():
            if not isinstance(subtask_res, dict):
                continue

            # 获取该子任务的配置
            subtask_cfg = stage_config.get(subtask_name, {})
            if not subtask_cfg:
                continue

            mode = subtask_cfg.get("mode", "consecutive")

            # 根据模式执行时序逻辑
            event: Optional[str] = None
            if mode == "consecutive":
                event = self._analyze_consecutive(
                    state, subtask_name, subtask_res, subtask_cfg
                )
            elif mode == "accumulated":
                event = self._analyze_accumulated(
                    state, subtask_name, subtask_res, subtask_cfg
                )
            elif mode == "sliding_window":
                event = self._analyze_sliding_window(
                    state, subtask_name, subtask_res, subtask_cfg, timestamp
                )

            if event:
                events.append(event)

        # 判断步骤是否完成
        step_completed = state.is_step_completed()

        # 获取状态快照
        state_snapshot = state.to_dict()

        return TemporalAnalysisResult(
            client_id=result.client_id,
            timestamp=timestamp,
            stage_changed=stage_changed,
            new_stage=new_stage,
            step_completed=step_completed,
            events=events,
            state_snapshot=state_snapshot,
        )

    def _analyze_consecutive(
        self,
        state: ClientState,
        subtask_name: str,
        subtask_res: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Optional[str]:
        """连续帧检测模式。

        Args:
            state: 客户端状态
            subtask_name: 子任务名称
            subtask_res: 子任务结果
            config: 配置

        Returns:
            事件描述（如果触发）
        """
        detected_key = f"{subtask_name}_detected"
        detected = subtask_res.get(detected_key, False)
        threshold = config.get("threshold", 3)

        counter_key = f"continuous_{subtask_name}"

        if detected:
            count = state.increment_counter(counter_key)
            if count >= threshold:
                state.mark_step_completed()
                return f"连续{count}帧检测到{subtask_name}"
        else:
            state.reset_counter(counter_key)

        return None

    def _analyze_accumulated(
        self,
        state: ClientState,
        subtask_name: str,
        subtask_res: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Optional[str]:
        """累计计数模式。

        Args:
            state: 客户端状态
            subtask_name: 子任务名称
            subtask_res: 子任务结果
            config: 配置

        Returns:
            事件描述（如果触发）
        """
        detected_key = f"{subtask_name}_detected"
        detected = subtask_res.get(detected_key, False)
        threshold = config.get("threshold", 5)

        counter_key = f"total_{subtask_name}"

        if detected:
            count = state.increment_counter(counter_key)
            if count >= threshold:
                state.mark_step_completed()
                return f"累计{count}次检测到{subtask_name}"

        return None

    def _analyze_sliding_window(
        self,
        state: ClientState,
        subtask_name: str,
        subtask_res: Dict[str, Any],
        config: Dict[str, Any],
        current_timestamp: float,
    ) -> Optional[str]:
        """滑动窗口模式（支持2秒时间窗口）。

        Args:
            state: 客户端状态
            subtask_name: 子任务名称
            subtask_res: 子任务结果
            config: 配置
            current_timestamp: 当前时间戳

        Returns:
            事件描述（如果触发）
        """
        detected_key = f"{subtask_name}_detected"
        detected = subtask_res.get(detected_key, False)

        # 配置参数
        window_seconds = config.get("window_seconds", 2.0)  # 默认2秒窗口
        ratio_threshold = config.get("ratio", 0.7)  # 默认70%比例

        history_key = f"{subtask_name}_window"

        # 1. 追加当前检测结果到历史
        state.push_temporal_history(
            key=history_key,
            value=detected,
            timestamp=current_timestamp,
            window_seconds=window_seconds,
        )

        # 2. 获取窗口内的所有检测结果
        values = state.get_temporal_values(
            key=history_key,
            timestamp=current_timestamp,
            window_seconds=window_seconds,
        )

        # 3. 计算窗口内的检测比例
        if len(values) == 0:
            return None

        detected_count = sum(1 for v in values if v)
        detection_ratio = detected_count / len(values)

        # 4. 判断是否满足阈值
        if detection_ratio >= ratio_threshold:
            state.mark_step_completed()
            return (
                f"滑动窗口检测：最近{window_seconds}秒内{len(values)}帧，"
                f"检测到{detected_count}次{subtask_name}（{detection_ratio:.1%}）"
            )

        return None
