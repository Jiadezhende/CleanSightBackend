"""推理任务基类

Task-Centric 架构：每个 InferenceWorkflow 负责完整的推理流程
- 检测 (infer)
- 时序分析 (analyze_temporal)：含边沿去抖 + 告警评估
- 可视化数据准备 (prepare_visualization_data)
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple

import numpy as np

from app.services.client.state import ClientState
from app.services.inference.data_models import (
    AlarmInfo,
    Detection,
    DetectionOutput,
    VisualizationData,
)

logger = logging.getLogger(__name__)


class InferenceWorkflow(ABC):
    """推理任务基类

    新架构设计：
    1. Task 内部组装检测策略（DetectionStrategy）和输出适配器（OutputAdapter）
    2. 检测输出统一为 DetectionOutput 格式
    3. 时序分析 + 告警评估合并为 analyze_temporal()，含边沿去抖
    4. 可视化数据由 Task 准备（prepare_visualization_data），渲染由固定渲染器完成

    子类只需实现 3 个核心方法：
    - infer(): 执行检测
    - analyze_temporal(): 时序分析 + 告警评估（边沿触发）
    - prepare_visualization_data(): 准备可视化数据
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
        window: List[DetectionOutput],
        state: ClientState,
    ) -> Tuple[List[str], List[AlarmInfo]]:
        """时序分析完整管道（含边沿去抖 + 告警评估）

        流程：
        1. 分析窗口数据，计算时序特征（连续帧 / 比例）
        2. 生成事件列表（前端展示）
        3. 更新 state 计数器 + 边沿去抖
        4. 仅在 rising-edge 时产出 AlarmInfo

        Args:
            window: 滑动窗口快照 [DetectionOutput, ...]，按时间升序
            state: 客户端状态（用于计数器管理和边沿触发标记）

        Returns:
            (events, alarms) — events 给前端展示，alarms 给 persistence
        """
        pass

    @abstractmethod
    def prepare_visualization_data(
        self,
        output: DetectionOutput,
    ) -> VisualizationData:
        """准备可视化数据

        基于检测输出准备可视化数据（检测框、标签、状态栏文本等），
        由固定渲染器负责绘制。

        Args:
            output: 检测输出

        Returns:
            VisualizationData: 可视化数据
        """
        pass

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


# ====== YOLO 工作流基类 ======

class YOLOWorkflow(InferenceWorkflow):
    """YOLO 检测工作流基类

    将 YOLO 模型加载、推理、输出适配整合进基类，消除各 workflow 的重复样板代码。
    infer() 的结果强制包装为 DetectionOutput。

    子类只需实现：
    - analyze_temporal()
    - prepare_visualization_data()

    如有自定义输出格式（如带 mask 的分割模型），可 override _adapt_output()。
    """

    def __init__(
        self,
        name: str,
        model_path: str,
        conf_threshold: float = 0.5,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(name=name, enabled=enabled)
        if not model_path:
            raise ValueError(f"model_path is required for {self.__class__.__name__}")
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self._model: Any = None

    # ====== YOLO 基础设施 ======

    def _ensure_model_loaded(self) -> None:
        """延迟加载 YOLO 模型"""
        if self._model is not None:
            return
        try:
            from pathlib import Path
            from ultralytics import YOLO

            if not Path(self.model_path).exists():
                raise FileNotFoundError(f"模型文件不存在: {self.model_path}")
            logger.info(f"[{self.name}] Loading YOLO model: {self.model_path}")
            self._model = YOLO(self.model_path)
            logger.info(f"[{self.name}] Model loaded successfully")
        except ImportError:
            raise RuntimeError("ultralytics not installed: pip install ultralytics")
        except Exception as e:
            logger.error(f"[{self.name}] Model loading failed: {e}", exc_info=True)
            raise

    def _adapt_output(self, raw_output: Any, frame: np.ndarray, timestamp: float) -> DetectionOutput:
        """将 YOLO Results 转换为 DetectionOutput

        子类可 override 此方法以支持自定义输出格式（如分割 mask、关键点等）。

        Args:
            raw_output: ultralytics YOLO predict() 返回的 Results 列表
            frame: 输入图像（用于记录 frame_shape）
            timestamp: 时间戳

        Returns:
            DetectionOutput: 标准化检测输出
        """
        detections = []
        try:
            if raw_output and len(raw_output) > 0:
                result = raw_output[0]
                if result.boxes is not None and len(result.boxes) > 0:
                    boxes = result.boxes.cpu().numpy()
                    class_names = result.names if hasattr(result, "names") else {}
                    for box in boxes:
                        xyxy = box.xyxy[0]
                        cls = int(box.cls[0])
                        detections.append(Detection(
                            bbox=[int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])],
                            confidence=float(box.conf[0]),
                            class_id=cls,
                            class_name=class_names.get(cls, f"class_{cls}"),
                        ))
        except Exception as e:
            logger.error(f"[{self.name}] Output adaptation failed: {e}", exc_info=True)

        return DetectionOutput(
            detections=detections,
            metadata={"model": "yolo", "frame_shape": frame.shape},
            timestamp=timestamp,
        )

    def _run_yolo(self, frame: np.ndarray) -> DetectionOutput:
        """单帧 YOLO 推理，结果通过 _adapt_output 包装为 DetectionOutput"""
        self._ensure_model_loaded()
        raw = self._model.predict(
            frame, conf=self.conf_threshold, iou=self.iou_threshold, verbose=False
        )
        return self._adapt_output(raw, frame, time.time())

    def _run_yolo_batch(self, frames: List[np.ndarray]) -> List[DetectionOutput]:
        """批量 YOLO 推理，每帧均通过 _adapt_output 包装为 DetectionOutput"""
        self._ensure_model_loaded()
        raw_list = self._model.predict(
            frames, conf=self.conf_threshold, iou=self.iou_threshold, verbose=False
        )
        timestamp = time.time()
        return [self._adapt_output([r], frame, timestamp) for r, frame in zip(raw_list, frames)]

    # ====== 核心方法默认实现 ======

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        """执行 YOLO 推理，结果强制包装为 DetectionOutput"""
        from app.utils.exceptions import ModelInferenceError
        try:
            return self._run_yolo(frame)
        except RuntimeError as e:
            error_msg = str(e).lower()
            raise ModelInferenceError(
                message=str(e),
                model_name=self.name,
                client_id=context.get("client_id"),
                is_cuda_error="out of memory" in error_msg or "cuda" in error_msg,
            ) from e
        except Exception as e:
            raise ModelInferenceError(
                message=f"Unexpected error in {self.name} detection: {str(e)}",
                model_name=self.name,
                client_id=context.get("client_id"),
            ) from e

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        """批量 YOLO 推理，降级时回退到逐帧 infer()"""
        try:
            outputs = self._run_yolo_batch(frames)
            for output in outputs:
                output.success = True
            return outputs
        except Exception as e:
            logger.error(f"[{self.name}] Batch inference failed, fallback to single: {e}", exc_info=True)
            return super().infer_batch(frames, contexts)

    def set_thresholds(
        self,
        conf_threshold: float | None = None,
        iou_threshold: float | None = None,
    ) -> None:
        """动态调整检测阈值"""
        if conf_threshold is not None:
            self.conf_threshold = max(0.0, min(1.0, conf_threshold))
        if iou_threshold is not None:
            self.iou_threshold = max(0.0, min(1.0, iou_threshold))
