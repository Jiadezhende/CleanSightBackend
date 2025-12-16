"""
YOLO 气泡检测服务

使用 YOLOv8 模型检测内镜清洗过程中的气泡
"""

import cv2
import numpy as np
from typing import Dict, Any, List, Tuple, Optional
from pathlib import Path


class BubbleDetector:
    """气泡检测器"""
    
    def __init__(self, model_path: Optional[str] = None):
        """
        初始化气泡检测器
        
        Args:
            model_path: YOLO 模型文件路径，如果为 None 则从配置读取
        """
        if model_path is None:
            from app.config import settings
            # 使用专门的气泡检测模型路径配置
            model_path = getattr(settings, 'bubble_model_path', settings.yolo_model_path)
            
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
            
            print(f"正在加载气泡检测模型: {self.model_path}")
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
            frame,
            conf=conf_threshold,
            iou=iou_threshold,
            verbose=False
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
                        "bbox": [int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])],
                        "confidence": conf,
                        "class_id": cls,
                        "class_name": class_name,
                    }
                    detections.append(detection)
        
        return detections, bubble_detected, bubble_count


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
