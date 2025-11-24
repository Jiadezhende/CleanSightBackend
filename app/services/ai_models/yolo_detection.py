"""
YOLO 内镜弯折检测服务

使用 YOLOv8 模型检测内镜是否弯折
"""

import cv2
import numpy as np
from typing import Dict, Any, List, Tuple, Optional
from pathlib import Path


class EndoscopeBendingDetector:
    """内镜弯折检测器"""
    
    def __init__(self, model_path: Optional[str] = None):
        """
        初始化内镜弯折检测器
        
        Args:
            model_path: YOLO 模型文件路径，如果为 None 则从配置读取
        """
        if model_path is None:
            from app.config import settings
            model_path = settings.yolo_model_path
            
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
            if hasattr(self.model, 'names'):
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
        iou_threshold: float = 0.45
    ) -> Tuple[np.ndarray, List[Dict[str, Any]], bool]:
        """
        检测内镜是否弯折
        
        Args:
            frame: 输入图像
            conf_threshold: 置信度阈值
            iou_threshold: IOU 阈值
            
        Returns:
            (标注后的帧, 检测结果列表, 是否检测到弯折)
        """
        if self.model is None:
            raise RuntimeError("模型未加载")
        
        # 执行推理
        results = self.model.predict(
            frame,
            conf=conf_threshold,
            iou=iou_threshold,
            verbose=False
        )
        
        # 解析结果
        detections = []
        annotated_frame = frame.copy()
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
                        "bbox": [int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])],
                        "confidence": conf,
                        "class_id": cls,
                        "class_name": class_name,
                    }
                    detections.append(detection)
                    
                    # 检查是否为弯折类别（根据您的模型类别定义）
                    # 假设模型训练时"bent"或"bending"表示弯折
                    if "bent" in class_name.lower() or "bending" in class_name.lower():
                        bending_detected = True
                    
                    # 在帧上绘制检测框
                    x1, y1, x2, y2 = detection["bbox"]
                    color = (0, 0, 255) if bending_detected else (0, 255, 0)
                    
                    # 绘制边界框
                    cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
                    
                    # 绘制标签
                    label = f"{class_name} {conf:.2f}"
                    label_size, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                    label_y = max(y1 - 10, label_size[1])
                    
                    # 标签背景
                    cv2.rectangle(
                        annotated_frame,
                        (x1, label_y - label_size[1] - 5),
                        (x1 + label_size[0], label_y + 5),
                        color,
                        -1
                    )
                    
                    # 标签文字
                    cv2.putText(
                        annotated_frame,
                        label,
                        (x1, label_y),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (255, 255, 255),
                        1
                    )
        
        return annotated_frame, detections, bending_detected


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
