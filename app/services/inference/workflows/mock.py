"""Mock 检测任务 —— 无荷载 CPU 试运行示例

用途：
    在没有真实模型权重的 CPU 服务器上验证整条推理链路：
    InferenceWorkflow → MultiModelWorkerPool → 时序分析 → 可视化

实现原理：
    不依赖 YOLO / torch / ultralytics。
    直接继承 InferenceWorkflow 基类，用纯 numpy 图像亮度启发式
    模拟检测结果，完整走通三个核心接口：
    1. infer()                    —— 返回合成 DetectionOutput
    2. analyze_temporal()         —— 连续帧边沿触发告警（与 bubble 相同模式）
    3. prepare_visualization_data()—— 生成状态栏覆盖层

替换为真实模型时：
    改为继承 YOLOWorkflow，删除 infer() / infer_batch() 覆盖，
    其余 analyze_temporal / prepare_visualization_data 保持不变。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple

import numpy as np

from app.services.client.state import ClientState
from app.services.inference.workflows.infer_workflow import InferenceWorkflow
from app.services.inference.data_models import (
    AlarmInfo,
    Detection,
    DetectionOutput,
    VisualizationData,
    VisItem,
    VisualizationType,
)

logger = logging.getLogger(__name__)

# 模拟检测的虚拟类别
_MOCK_CLASS_ID = 0
_MOCK_CLASS_NAME = "mock_object"


class MockDetectionTask(InferenceWorkflow):
    """无荷载 Mock 检测任务，用于 CPU 服务器试运行。

    检测逻辑（纯 numpy，无模型）：
        取帧中心 1/4 区域的灰度均值。
        均值 < brightness_threshold → 视为"检测到目标"（模拟暗区异常）。
        置信度 = 1.0 - mean/255（均值越低，置信度越高）。

    时序逻辑：
        连续 consecutive_trigger 帧检测到目标 → 触发事件，边沿上报告警。
    """

    def __init__(
        self,
        brightness_threshold: float = 100.0,
        consecutive_trigger: int = 3,
        enabled: bool = True,
    ):
        """
        Args:
            brightness_threshold: 中心区域灰度均值阈值。低于此值视为检测到目标。
            consecutive_trigger:  连续检测到目标的帧数阈值，达到后触发事件。
            enabled:              是否启用此任务。
        """
        super().__init__(name="mock", enabled=enabled)
        self.brightness_threshold = brightness_threshold
        self.consecutive_trigger = consecutive_trigger

    # ====== 1. 检测（纯 numpy，无模型） ======

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        """单帧检测：基于中心区域亮度启发式生成合成检测结果。"""
        timestamp = time.time()

        # 取中心 1/4 区域
        h, w = frame.shape[:2]
        cy1, cy2 = h // 4, 3 * h // 4
        cx1, cx2 = w // 4, 3 * w // 4
        center_crop = frame[cy1:cy2, cx1:cx2]

        # 计算灰度均值
        if center_crop.ndim == 3:
            gray = np.mean(center_crop, axis=2)
        else:
            gray = center_crop.astype(float)
        mean_brightness = float(np.mean(gray))

        detections: List[Detection] = []

        if mean_brightness < self.brightness_threshold:
            confidence = 1.0 - mean_brightness / 255.0
            detections.append(Detection(
                bbox=[cx1, cy1, cx2, cy2],
                confidence=round(confidence, 4),
                class_id=_MOCK_CLASS_ID,
                class_name=_MOCK_CLASS_NAME,
                extra={"mean_brightness": round(mean_brightness, 2)},
            ))

        return DetectionOutput(
            detections=detections,
            metadata={
                "model": "mock_brightness",
                "frame_shape": frame.shape,
                "mean_brightness": round(mean_brightness, 2),
            },
            timestamp=timestamp,
            success=True,
        )

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        """批量检测：逐帧调用 infer()（无 GPU，顺序执行）。"""
        results = []
        for frame, ctx in zip(frames, contexts):
            output = self.infer(frame, ctx)
            results.append(output)
        return results

    # ====== 2. 时序分析（连续帧边沿触发，与 bubble 相同模式） ======

    def analyze_temporal(
        self,
        window: List[DetectionOutput],
        state: ClientState,
    ) -> Tuple[List[str], List[AlarmInfo]]:
        """时序分析：连续 N 帧检测到目标 → 边沿触发告警。"""
        if not window:
            return [], []

        # ① 计算时序特征：从最新帧往前统计连续检测到目标的帧数
        consecutive = 0
        for output in reversed(window):
            if len(output.detections) > 0:
                consecutive += 1
            else:
                break

        # ② 更新累计计数器
        latest = window[-1]
        if len(latest.detections) > 0:
            state.increment_counter("mock_total", delta=len(latest.detections))

        # ③ 判断是否触发事件
        is_triggered = consecutive >= self.consecutive_trigger
        events = [f"连续{consecutive}帧检测到 mock_object"] if is_triggered else []

        # ④ 边沿触发：只在 0→1 跳变时投递告警
        alarms: List[AlarmInfo] = []
        was_alarming = state.get_counter("mock_alarming", 0) > 0

        if is_triggered and not was_alarming:
            state.increment_counter("mock_alarming")
            state.increment_counter("mock_alarm_count")
            alarms.append(AlarmInfo(
                alarm_type="Mock告警",
                alarm_level="low",
                alarm_message=f"Mock检测触发（连续{consecutive}帧）",
                metadata={
                    "consecutive_frames": consecutive,
                    "brightness": latest.metadata.get("mean_brightness"),
                },
            ))
        elif not is_triggered and was_alarming:
            # 下降沿：复位，允许下次重新触发
            state.reset_counter("mock_alarming")

        return events, alarms

    # ====== 3. 可视化数据准备 ======

    def prepare_visualization_data(
        self, output: DetectionOutput,
    ) -> VisualizationData:
        """准备可视化数据：绘制检测框 + 状态栏。"""
        items = [
            VisItem(
                bbox=det.bbox,
                label=f"[MOCK] {det.confidence:.2f}",
                confidence=det.confidence,
                color=(255, 128, 0),   # 橙色，便于与真实检测框区分
            )
            for det in output.detections
        ]

        detected = len(output.detections) > 0
        brightness = output.metadata.get("mean_brightness", "-")

        if detected:
            status_text = f"[MOCK] Detected (lum={brightness})"
            status_color = (0, 128, 255)   # 橙色
        else:
            status_text = f"[MOCK] Clear (lum={brightness})"
            status_color = (0, 200, 0)     # 绿色

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )
