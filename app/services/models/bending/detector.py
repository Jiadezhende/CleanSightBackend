"""
YOLO 内镜弯折检测服务

使用 YOLOv8 模型检测内镜是否弯折
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np


class EndoscopeBendingDetector:
    """内镜弯折检测器"""

    def __init__(self, model_path: Optional[str] = None):
        """
        初始化内镜弯折检测器

        Args:
            model_path: YOLO 模型文件路径，如果为 None 则从配置读取
        """
        if model_path is None:
            raise ValueError("model_path is required for EndoscopeBendingDetector")

        self.model_path = model_path
        self.model = None
        self.class_names = {}
        self._load_model()

    def _load_model(self):
        """加载 YOLO 模型"""
        try:
            from ultralytics import YOLO

            model_path = Path(self.model_path)
            if not model_path.exists():
                raise FileNotFoundError(f"模型文件不存在: {self.model_path}")

            print(f"正在加载内镜弯折检测模型: {self.model_path}")
            self.model = YOLO(self.model_path)

            # 获取类别名称
            if hasattr(self.model, "names"):
                self.class_names = self.model.names

            print(f"模型加载成功，类别数量: {len(self.class_names)}")
            print(f"检测类别: {self.class_names}")

        except ImportError:
            print("错误: 未安装 ultralytics 库，请运行: pip install ultralytics")
            raise
        except Exception as e:
            print(f"模型加载失败: {e}")
            raise

    def detect(
        self,
        frame: np.ndarray,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        检测内镜是否弯折（仅返回检测结果，不绘制）

        Args:
            frame: 输入图像
            conf_threshold: 置信度阈值
            iou_threshold: IOU 阈值

        Returns:
            (检测结果列表, 是否检测到弯折)
        """
        if self.model is None:
            raise RuntimeError("模型未加载")

        # 执行推理
        results = self.model.predict(
            frame, conf=conf_threshold, iou=iou_threshold, verbose=False
        )

        # 解析结果
        detections = []
        bending_detected = False

        if results and len(results) > 0:
            result = results[0]

            # 获取检测框
            if result.boxes is not None and len(result.boxes) > 0:
                boxes = result.boxes.cpu().numpy()

                for box in boxes:
                    # 提取信息
                    xyxy = box.xyxy[0]  # [x1, y1, x2, y2]
                    conf = float(box.conf[0])
                    cls = int(box.cls[0])
                    class_name = self.class_names.get(cls, f"class_{cls}")

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

                    # 检查是否为弯折类别（根据您的模型类别定义）
                    # 假设模型训练时"bent"或"bending"表示弯折
                    if "bent" in class_name.lower() or "bending" in class_name.lower():
                        bending_detected = True
        # 如果没有任何真实检测结果，为了调试可视化管线，添加一个默认调试框
        # 注意：不修改 bending_detected，避免影响业务逻辑
        if not detections:
            try:
                h, w = frame.shape[:2]
                # 弯折模型调试框放在左上角区域，以区别于气泡模型的中心调试框
                x1, y1 = w // 10, h // 10
                x2, y2 = w // 3, h // 3
                debug_det = {
                    "bbox": [int(x1), int(y1), int(x2), int(y2)],
                    "confidence": 0.0,
                    "class_id": -1,
                    # 专门用于区分弯折检测模型的调试框
                    "class_name": "bending_debug_box",
                }
                detections.append(debug_det)
            except Exception:
                pass

        return detections, bending_detected

    def detect_batch(
        self,
        frames: List[np.ndarray],
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
    ) -> List[Tuple[List[Dict[str, Any]], bool]]:
        """对一批图像执行检测，返回每帧的 (detections, bending_detected)。
        使用 ultralytics 的 batch 支持以减少 GPU 调用开销。
        """
        if self.model is None:
            raise RuntimeError("模型未加载")

        results = self.model.predict(
            frames, conf=conf_threshold, iou=iou_threshold, verbose=False
        )

        out = []
        if not results:
            # 返回每帧一个默认调试框，便于确认管线是否在运行
            for frame in frames:
                try:
                    h, w = frame.shape[:2]
                    # 弯折模型调试框：左上角
                    x1, y1 = w // 10, h // 10
                    x2, y2 = w // 3, h // 3
                    debug_det = {
                        "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        "confidence": 0.0,
                        "class_id": -1,
                        # 专门用于区分弯折检测模型的调试框
                        "class_name": "bending_debug_box",
                    }
                    out.append(([debug_det], False))
                except Exception:
                    out.append(([], False))
            return out

        # results 与输入 frames 一一对应
        for result, frame in zip(results, frames):
            detections = []
            bending_detected = False
            if (
                result is not None
                and result.boxes is not None
                and len(result.boxes) > 0
            ):
                boxes = result.boxes.cpu().numpy()
                for box in boxes:
                    xyxy = box.xyxy[0]
                    conf = float(box.conf[0])
                    cls = int(box.cls[0])
                    class_name = self.class_names.get(cls, f"class_{cls}")
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
                    if "bent" in class_name.lower() or "bending" in class_name.lower():
                        bending_detected = True
            # 若本帧无任何真实检测结果时，也添加一个默认调试框
            if not detections:
                try:
                    h, w = frame.shape[:2]
                    # 弯折模型调试框：左上角
                    x1, y1 = w // 10, h // 10
                    x2, y2 = w // 3, h // 3
                    debug_det = {
                        "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        "confidence": 0.0,
                        "class_id": -1,
                        # 专门用于区分弯折检测模型的调试框
                        "class_name": "bending_debug_box",
                    }
                    detections.append(debug_det)
                except Exception:
                    pass
            out.append((detections, bending_detected))

        return out


# 单例模式
_detector_instance = None


def get_detector(model_path: str = None) -> EndoscopeBendingDetector:
    """
    获取内镜弯折检测器单例

    Args:
        model_path: 模型路径（可选，如果为 None 则从配置读取）

    Returns:
        EndoscopeBendingDetector 实例
    """
    global _detector_instance

    if _detector_instance is None:
        _detector_instance = EndoscopeBendingDetector(model_path)

    return _detector_instance
