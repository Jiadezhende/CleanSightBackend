"""
YOLO 气泡检测服务

使用 YOLOv8 模型检测内镜清洗过程中的气泡
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class BubbleDetector:
    """气泡检测器"""

    def __init__(self, model_path: Optional[str] = None):
        """
        初始化气泡检测器

        Args:
            model_path: YOLO 模型文件路径，如果为 None 则从配置读取
        """
        if model_path is None:
            raise ValueError("model_path is required for BubbleDetector")

        self.model_path = model_path
        self.model = None
        self.class_names = {}
        self._load_model()

        # 简单的 ANSI 颜色码（在支持 ANSI 的终端中生效）
        self._log_color_yellow = "\033[33m"
        self._log_color_reset = "\033[0m"

    def _load_model(self):
        """加载 YOLO 模型"""
        try:
            from ultralytics import YOLO

            model_path = Path(self.model_path)
            if not model_path.exists():
                raise FileNotFoundError(f"模型文件不存在: {self.model_path}")

            logger.debug("[BubbleDetector] Loading model: %s", self.model_path)
            self.model = YOLO(self.model_path)

            # 获取类别名称
            if hasattr(self.model, "names"):
                self.class_names = self.model.names

            logger.info("[BubbleDetector] Model loaded: %s | classes=%d", 
                       self.model_path, len(self.class_names))
            logger.debug("[BubbleDetector] Class names: %s", self.class_names)

        except ImportError:
            logger.error("[BubbleDetector] ✖ ultralytics library not installed")
            raise
        except Exception as e:
            logger.error("[BubbleDetector] ✖ Model loading failed: %s", e, exc_info=True)
            raise

    def detect(
        self,
        frame: np.ndarray,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
    ) -> Tuple[List[Dict[str, Any]], bool, int]:
        """
        检测气泡（仅返回检测结果，不绘制）

        Args:
            frame: 输入图像
            conf_threshold: 置信度阈值
            iou_threshold: IOU 阈值

        Returns:
            (检测结果列表, 是否检测到气泡, 气泡数量)
        """
        if self.model is None:
            raise RuntimeError("模型未加载")

        # 执行推理
        results = self.model.predict(
            frame, conf=conf_threshold, iou=iou_threshold, verbose=False
        )

        # 解析结果
        detections = []
        bubble_detected = False
        bubble_count = 0

        if results and len(results) > 0:
            result = results[0]

            # 获取检测框
            if result.boxes is not None and len(result.boxes) > 0:
                boxes = result.boxes.cpu().numpy()
                bubble_count = len(boxes)
                bubble_detected = True

                for box in boxes:
                    # 提取信息
                    xyxy = box.xyxy[0]  # [x1, y1, x2, y2]
                    conf = float(box.conf[0])
                    cls = int(box.cls[0])
                    class_name = self.class_names.get(cls, f"bubble")

                    # 构建检测结果
                    detection = {
                        "bbox": [
                            int(xyxy[0]),
                            int(xyxy[1]),
                            int(xyxy[2]),
                            int(xyxy[3]),
                        ],
                        "confidence": conf,
                        "class_id": cls,
                        "class_name": class_name,
                    }
                    detections.append(detection)
        # 如果没有任何真实检测结果，为了调试可视化管线，添加一个默认调试框
        # 注意：这里不修改 bubble_detected / bubble_count，避免影响业务逻辑
        if not detections:
            try:
                h, w = frame.shape[:2]
                x1, y1 = w // 4, h // 4
                x2, y2 = 3 * w // 4, 3 * h // 4
                debug_det = {
                    "bbox": [int(x1), int(y1), int(x2), int(y2)],
                    "confidence": 0.0,
                    "class_id": -1,
                    # 专门用于区分气泡模型的调试框
                    "class_name": "bubble_debug_box",
                }
                detections.append(debug_det)
            except Exception:
                pass

        return detections, bubble_detected, bubble_count

    def detect_batch(
        self,
        frames: List[np.ndarray],
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
    ) -> List[Tuple[List[Dict[str, Any]], bool, int]]:
        """对一批图像执行气泡检测，返回每帧的 (detections, bubble_detected, bubble_count)。"""
        if self.model is None:
            raise RuntimeError("模型未加载")

        results = self.model.predict(
            frames, conf=conf_threshold, iou=iou_threshold, verbose=False
        )

        out = []
        if not results:
            for frame in frames:
                # 无结果时仍然添加默认Debug框，便于确认管线是否工作
                try:
                    h, w = frame.shape[:2]
                    x1, y1 = w // 4, h // 4
                    x2, y2 = 3 * w // 4, 3 * h // 4
                    debug_det = {
                        "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        "confidence": 0.0,
                        "class_id": -1,
                        # 专门用于区分气泡模型的调试框
                        "class_name": "bubble_debug_box",
                    }
                    out.append(([debug_det], False, 0))
                except Exception:
                    out.append(([], False, 0))
            return out

        for result, frame in zip(results, frames):
            detections = []
            bubble_detected = False
            bubble_count = 0
            if (
                result is not None
                and result.boxes is not None
                and len(result.boxes) > 0
            ):
                boxes = result.boxes.cpu().numpy()
                bubble_count = len(boxes)
                bubble_detected = True
                for box in boxes:
                    xyxy = box.xyxy[0]
                    conf = float(box.conf[0])
                    cls = int(box.cls[0])
                    class_name = self.class_names.get(cls, f"bubble")
                    detection = {
                        "bbox": [
                            int(xyxy[0]),
                            int(xyxy[1]),
                            int(xyxy[2]),
                            int(xyxy[3]),
                        ],
                        "confidence": conf,
                        "class_id": cls,
                        "class_name": class_name,
                    }
                    detections.append(detection)
                    # 带颜色的调试输出（黄色），便于在日志中快速识别，有检测框时才会输出
                    try:
                        print(
                            f"{self._log_color_yellow}[Raw Detection]: {detection}{self._log_color_reset}"
                        )
                    except Exception:
                        # 极端情况下（如终端不支持 ANSI），退回普通打印
                        print(f"[Raw Detection]: {detection}")
            # 若本帧没有任何真实检测结果，追加一个默认调试框，方便确认模型/管线已运行
            if not detections:
                try:
                    h, w = frame.shape[:2]
                    x1, y1 = w // 4, h // 4
                    x2, y2 = 3 * w // 4, 3 * h // 4
                    debug_det = {
                        "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        "confidence": 0.0,
                        "class_id": -1,
                        # 专门用于区分气泡模型的调试框
                        "class_name": "bubble_debug_box",
                    }
                    detections.append(debug_det)
                except Exception:
                    pass
            out.append((detections, bubble_detected, bubble_count))

        return out


# 单例模式
_bubble_detector_instance = None


def get_bubble_detector(model_path: str = None) -> BubbleDetector:
    """
    获取气泡检测器单例

    Args:
        model_path: 模型路径（可选，如果为 None 则从配置读取）

    Returns:
        BubbleDetector 实例
    """
    global _bubble_detector_instance

    if _bubble_detector_instance is None:
        _bubble_detector_instance = BubbleDetector(model_path)

    return _bubble_detector_instance
