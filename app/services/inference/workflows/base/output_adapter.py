"""输出适配器

将不同模型的原始输出转换为标准 DetectionOutput 格式。
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, List

import numpy as np

from app.services.inference.data_models import Detection, DetectionOutput

logger = logging.getLogger(__name__)


class OutputAdapter(ABC):
    """输出适配器抽象基类
    
    将原始模型输出转换为标准 DetectionOutput 格式
    """
    
    @abstractmethod
    def adapt(self, raw_output: Any, frame: np.ndarray, timestamp: float) -> DetectionOutput:
        """适配原始输出
        
        Args:
            raw_output: 原始模型输出
            frame: 输入图像（用于获取尺寸等信息）
            timestamp: 时间戳
            
        Returns:
            DetectionOutput: 标准化检测输出
        """
        pass


class YOLOAdapter(OutputAdapter):
    """YOLO 输出适配器
    
    将 ultralytics YOLO 的 Results 对象转换为标准格式
    """
    
    def adapt(self, raw_output: Any, frame: np.ndarray, timestamp: float) -> DetectionOutput:
        """适配 YOLO Results 对象
        
        Args:
            raw_output: ultralytics YOLO predict() 返回的 Results 列表
            frame: 输入图像
            timestamp: 时间戳
            
        Returns:
            DetectionOutput: 标准化检测输出
        """
        detections = []
        
        try:
            # YOLO predict 返回的是列表，取第一个元素
            if raw_output and len(raw_output) > 0:
                result = raw_output[0]
                
                # 检查是否有检测框
                if result.boxes is not None and len(result.boxes) > 0:
                    boxes = result.boxes.cpu().numpy()
                    
                    # 获取类别名称
                    class_names = result.names if hasattr(result, 'names') else {}
                    
                    for box in boxes:
                        # 提取边界框坐标
                        xyxy = box.xyxy[0]  # [x1, y1, x2, y2]
                        conf = float(box.conf[0])
                        cls = int(box.cls[0])
                        class_name = class_names.get(cls, f"class_{cls}")
                        
                        # 创建 Detection 对象
                        detection = Detection(
                            bbox=[int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])],
                            confidence=conf,
                            class_id=cls,
                            class_name=class_name
                        )
                        detections.append(detection)
        
        except Exception as e:
            logger.error(f"[YOLOAdapter] Error adapting YOLO output: {e}", exc_info=True)
        
        return DetectionOutput(
            detections=detections,
            metadata={"model": "yolo", "frame_shape": frame.shape},
            timestamp=timestamp
        )


class TransformerAdapter(OutputAdapter):
    """Transformer 输出适配器（预留）
    
    未来用于适配 DETR、DINO 等 Transformer 模型的输出
    """
    
    def adapt(self, raw_output: Any, frame: np.ndarray, timestamp: float) -> DetectionOutput:
        """适配 Transformer 模型输出"""
        # TODO: 实现 Transformer 输出适配逻辑
        raise NotImplementedError("TransformerAdapter not implemented yet")


# 适配器工厂（可选）
class AdapterFactory:
    """适配器工厂 - 根据类型创建适配器实例"""
    
    _adapters = {
        "yolo": YOLOAdapter,
        "transformer": TransformerAdapter,
    }
    
    @classmethod
    def create(cls, adapter_type: str) -> OutputAdapter:
        """创建适配器实例
        
        Args:
            adapter_type: 适配器类型 ("yolo", "transformer")
            
        Returns:
            OutputAdapter 实例
        """
        adapter_cls = cls._adapters.get(adapter_type.lower())
        if adapter_cls is None:
            raise ValueError(f"Unknown adapter type: {adapter_type}. Available: {list(cls._adapters.keys())}")
        
        return adapter_cls()
    
    @classmethod
    def register(cls, name: str, adapter_cls: type):
        """注册新适配器类型"""
        cls._adapters[name.lower()] = adapter_cls
