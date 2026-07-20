"""Detector — 无状态 GPU 推理基类

Detector 负责单帧/批量检测推理和可视化数据准备，不持有任何 per-client 状态。
同一个 Detector 实例可被所有 Client 的推理线程和可视化线程共享调用。

线程安全：Detector 无可变成员（除惰性加载的模型权重），可安全多线程访问。
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, List

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
    子类须实现 infer_batch() 和 prepare_visualization_data()。
    """

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    @abstractmethod
    def infer_batch(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[FrameDetections]:
        """批量推理（唯一推理入口）。

        Args:
            frames: BGR 图像列表
            timestamps: 各帧的帧捕获时间戳（真值锚点，源自 Frame.timestamp）。
                实现须把 timestamps[i] 原样写入 frames[i] 对应的
                FrameDetections.timestamp——写回口据此物化 FrameFeature（帧级多流对齐），
                帧窗算子用 FrameFeature.ts 裁窗、用 FrameDetections.timestamp 推进游标，
                二者须同源同值；detector 不得自造时间戳（否则内部对齐错乱）。

        Returns:
            List[FrameDetections]：与 frames 一一对应
        """

    @abstractmethod
    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        """根据检测输出准备可视化数据。

        Args:
            output: infer_batch() 的单帧输出

        Returns:
            RenderSpec：供 FixedVisualizer 渲染
        """


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
            metadata={"model": "yolo"},  # 帧分辨率上移 FrameInference.wh，不再逐检测器塞
            timestamp=timestamp,
        )

    def _run_yolo_batch(
        self, frames: List[np.ndarray], timestamps: List[float]
    ) -> List[FrameDetections]:
        """批量 YOLO 推理。timestamps[i] 为帧捕获真值锚点，写入对应 FrameDetections。"""
        self._ensure_model_loaded()
        # ultralytics 无条件 mkdir(save_dir)（即便 save=False），project/name/exist_ok
        # 把这个空目录钉进已 gitignore 的 .ultralytics 并复用同一个，避免污染仓库根与
        # predict/predict2… 累积（详见 app/settings.py:YOLO_RUNS_PROJECT）。
        from app.settings import YOLO_RUNS_PROJECT
        raw_list = self._model.predict(
            frames, conf=self.conf_threshold, iou=self.iou_threshold, verbose=False,
            project=YOLO_RUNS_PROJECT, name="predict", exist_ok=True,
        )
        return [
            self._adapt_output([r], frame, ts)
            for r, frame, ts in zip(raw_list, frames, timestamps)
        ]

    # ── 核心方法实现 ────────────────────────────────────────

    def infer_batch(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[FrameDetections]:
        """批量 YOLO 推理（唯一推理入口）。

        整批推理失败时逐帧返回 error 结果，仍保留各帧捕获 ts（不自造时间戳）。
        """
        try:
            outputs = self._run_yolo_batch(frames, timestamps)
            for output in outputs:
                output.success = True
            return outputs
        except Exception as e:
            logger.error(
                "[%s] Batch inference failed: %s", self.name, e, exc_info=True,
            )
            return [
                FrameDetections(
                    detections=[],
                    metadata={"error": str(e)},
                    timestamp=ts,
                    success=False,
                    error=str(e),
                )
                for ts in timestamps
            ]

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
