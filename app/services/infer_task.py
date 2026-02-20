from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

"""
推理任务基类及具体任务实现
"""

# Type aliases for better readability
InferenceResult = Dict[str, Any]  # 推理结果类型


class InferenceTask(ABC):
    """推理任务基类，所有推理任务都应继承此类。

    每个任务都是独立的，可以并行执行。
    """

    def __init__(self, name: str, enabled: bool = True):
        """
        Args:
            name: 任务名称
            enabled: 是否启用此任务
        """
        self.name = name
        self.enabled = enabled

    @abstractmethod
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行推理任务。

        Args:
            frame: 输入帧
            context: 上下文信息，包含其他任务的结果、任务对象等

        Returns:
            推理结果字典
        """
        pass

    @abstractmethod
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """在帧上可视化推理结果。

        Args:
            frame: 输入帧
            result: 推理结果

        Returns:
            可视化后的帧
        """
        pass

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[InferenceResult]:
        """可选的批量推理接口，默认逐帧串行调用 `infer`。

        子类可覆盖以利用模型的 batch 接口（例如 YOLO 的 list-of-images 输入）。
        """
        results: List[InferenceResult] = []
        for f, ctx in zip(frames, contexts):
            results.append(self.infer(f, ctx))
        return results

    def requires_context(self) -> List[str]:
        """返回此任务依赖的其他任务名称列表。

        Returns:
            依赖的任务名称列表，空列表表示无依赖
        """
        return []


class DetectionTask(InferenceTask):
    """关键点检测任务"""

    def __init__(self):
        super().__init__(name="detection", enabled=True)

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行检测推理"""
        try:
            # 延迟导入避免循环依赖
            from app.services.models.base import detect_keypoints

            # 调用检测模型
            processed_frame, keypoints = detect_keypoints(frame)
            return {
                "success": True,
                "processed_frame": processed_frame,  # 用于可视化，不会序列化
                "keypoints": keypoints,
            }
        except Exception as e:
            print(f"Detection task error: {e}")
            return {
                "success": False,
                "error": str(e),
                "processed_frame": frame.copy(),  # 用于可视化，不会序列化
                "keypoints": {},
            }

    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """可视化检测结果（检测模型已经画好了框）"""
        return result.get("processed_frame", frame)


class MotionTask(InferenceTask):
    """动作分析任务（依赖检测结果）"""

    def __init__(self):
        super().__init__(name="motion", enabled=True)

    def requires_context(self) -> List[str]:
        """依赖检测任务的结果"""
        return ["detection"]

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行动作分析"""
        try:
            # 延迟导入避免循环依赖
            from app.services.models.base import analyze_motion

            # 获取检测结果
            detection_result = context.get("results", {}).get("detection", {})
            keypoints = detection_result.get("keypoints", {})

            # 获取任务对象
            task = context.get("task")

            if not task or not keypoints:
                return {
                    "success": False,
                    "error": "Missing task or keypoints",
                    "actions": {},
                }

            # 调用动作分析模型
            actions = analyze_motion(keypoints, task)

            return {"success": True, "actions": actions}
        except Exception as e:
            print(f"Motion task error: {e}")
            return {"success": False, "error": str(e), "actions": {}}

    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """可视化动作分析结果"""
        if not result.get("success"):
            return frame

        result_frame = frame.copy()
        actions = result.get("actions", {})
        y_offset = 100  # 避免与检测结果重叠

        if actions.get("bending_detected"):
            cv2.putText(
                result_frame,
                "Bending Detected!",
                (10, y_offset),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 255),
                2,
            )
            y_offset += 25

        if actions.get("bubble_detected"):
            cv2.putText(
                result_frame,
                "Bubble Detected!",
                (10, y_offset),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 0),
                2,
            )
            y_offset += 25

        status = actions.get("submersion_status", "unknown")
        if status != "unknown":
            cv2.putText(
                result_frame,
                f"Status: {status}",
                (10, y_offset),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                2,
            )

        return result_frame
