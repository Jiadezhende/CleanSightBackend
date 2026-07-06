"""Detector — 无状态 GPU 推理基类

Detector 负责单帧/批量检测推理和可视化数据准备，不持有任何 per-client 状态。
同一个 Detector 实例可被所有 Client 的推理线程和可视化线程共享调用。

线程安全：Detector 无可变成员（除惰性加载的模型权重），可安全多线程访问。
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import numpy as np

from app.domain.detection import Detection, FrameDetections
from app.domain.render import RenderSpec

logger = logging.getLogger(__name__)


class Detector(ABC):
    """无状态推理检测器基类。

    职责：
    - 执行单帧或批量 GPU 推理，输出标准化 FrameDetections
    - 准备可视化数据（检测框、标签、状态栏文本等）

    不持有任何 per-client 状态。同一实例被所有 client 共享。
    子类须实现 infer() 和 prepare_visualization_data()。
    """

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    @abstractmethod
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> FrameDetections:
        """单帧推理。

        Args:
            frame: BGR 图像
            context: 请求上下文（含 client_id 等）

        Returns:
            FrameDetections：标准化检测输出
        """

    @abstractmethod
    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        """根据检测输出准备可视化数据。

        Args:
            output: infer()/infer_batch() 的输出

        Returns:
            RenderSpec：供 FixedVisualizer 渲染
        """

    def infer_batch(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]],
    ) -> List[FrameDetections]:
        """批量推理。默认实现：逐帧调用 infer()。

        子类可 override 以利用模型的原生 batch 接口加速（如 YOLO batch predict）。
        """
        results = []
        for frame, ctx in zip(frames, contexts):
            try:
                output = self.infer(frame, ctx)
                output.success = True
                results.append(output)
            except Exception as e:
                results.append(FrameDetections(
                    detections=[],
                    metadata={"error": str(e)},
                    timestamp=time.time(),
                    success=False,
                    error=str(e),
                ))
        return results


# ====== YOLO 检测器基类 ======

class YOLODetector(Detector):
    """基于 YOLO 的检测器基类。

    将 YOLO 模型加载、单帧推理、批量推理、输出适配整合进基类，
    消除各 Detector 子类的重复样板代码。

    子类只需实现 prepare_visualization_data()。
    如需自定义输出（如分割 mask），可 override _adapt_output()。
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
        self._model_load_lock = threading.Lock()

    # ── YOLO 基础设施 ──────────────────────────────────────

    def _ensure_model_loaded(self) -> None:
        """惰性加载 YOLO 模型（首次推理时触发，双重检查锁保证线程安全）。"""
        if self._model is not None:
            return
        with self._model_load_lock:
            if self._model is not None:
                return
            try:
                from pathlib import Path
                from ultralytics import YOLO

                if not Path(self.model_path).exists():
                    raise FileNotFoundError(f"模型文件不存在: {self.model_path}")
                logger.info("[%s] Loading YOLO model: %s", self.name, self.model_path)
                self._model = YOLO(self.model_path)
                logger.info("[%s] Model loaded successfully", self.name)
            except ImportError:
                raise RuntimeError("ultralytics not installed: pip install ultralytics")
            except Exception as e:
                logger.error("[%s] Model loading failed: %s", self.name, e, exc_info=True)
                raise

    def _adapt_output(
        self, raw_output: Any, frame: np.ndarray, timestamp: float
    ) -> FrameDetections:
        """将 YOLO Results 转换为 FrameDetections。

        子类可 override 以支持自定义输出格式（如分割 mask、关键点等）。
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
            logger.error("[%s] Output adaptation failed: %s", self.name, e, exc_info=True)

        return FrameDetections(
            detections=detections,
            metadata={"model": "yolo", "frame_shape": frame.shape},
            timestamp=timestamp,
        )

    def _run_yolo(self, frame: np.ndarray) -> FrameDetections:
        """单帧 YOLO 推理。"""
        self._ensure_model_loaded()
        raw = self._model.predict(
            frame, conf=self.conf_threshold, iou=self.iou_threshold, verbose=False
        )
        return self._adapt_output(raw, frame, time.time())

    def _run_yolo_batch(self, frames: List[np.ndarray]) -> List[FrameDetections]:
        """批量 YOLO 推理。"""
        self._ensure_model_loaded()
        raw_list = self._model.predict(
            frames, conf=self.conf_threshold, iou=self.iou_threshold, verbose=False
        )
        timestamp = time.time()
        return [self._adapt_output([r], frame, timestamp) for r, frame in zip(raw_list, frames)]

    # ── 核心方法实现 ────────────────────────────────────────

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> FrameDetections:
        from app.utils.exceptions import ModelInferenceError
        try:
            return self._run_yolo(frame)
        except RuntimeError as e:
            error_msg = str(e).lower()
            raise ModelInferenceError(
                message=str(e),
                model_name=self.name,
                task_id=context.get("task_id"),
                step_id=context.get("step_id"),
                source_ip=context.get("source_ip"),
                is_cuda_error="out of memory" in error_msg or "cuda" in error_msg,
            ) from e
        except Exception as e:
            raise ModelInferenceError(
                message=f"Unexpected error in {self.name} detection: {str(e)}",
                model_name=self.name,
                task_id=context.get("task_id"),
                step_id=context.get("step_id"),
                source_ip=context.get("source_ip"),
            ) from e

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[FrameDetections]:
        """批量 YOLO 推理，降级时回退到逐帧 infer()。"""
        try:
            outputs = self._run_yolo_batch(frames)
            for output in outputs:
                output.success = True
            return outputs
        except Exception as e:
            logger.error(
                "[%s] Batch inference failed, fallback to single: %s",
                self.name, e, exc_info=True,
            )
            return super().infer_batch(frames, contexts)

    def set_thresholds(
        self,
        conf_threshold: float | None = None,
        iou_threshold: float | None = None,
    ) -> None:
        """动态调整检测阈值。"""
        if conf_threshold is not None:
            self.conf_threshold = max(0.0, min(1.0, conf_threshold))
        if iou_threshold is not None:
            self.iou_threshold = max(0.0, min(1.0, iou_threshold))
