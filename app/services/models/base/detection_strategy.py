"""检测策略基类

封装不同检测算法（YOLO、Transformer等）的底层推理逻辑。
Task 内部使用，外部不直接感知。
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class DetectionStrategy(ABC):
    """检测策略抽象基类
    
    封装不同检测框架的模型加载和推理逻辑
    """
    
    @abstractmethod
    def load_model(self, model_path: str, **kwargs) -> None:
        """加载模型
        
        Args:
            model_path: 模型文件路径
            **kwargs: 额外参数（如设备、batch_size等）
        """
        pass
    
    @abstractmethod
    def detect(self, frame: np.ndarray, **kwargs) -> Any:
        """单帧检测
        
        Args:
            frame: 输入图像
            **kwargs: 检测参数（如conf_threshold、iou_threshold等）
            
        Returns:
            原始模型输出（由适配器转换为标准格式）
        """
        pass
    
    @abstractmethod
    def detect_batch(self, frames: List[np.ndarray], **kwargs) -> List[Any]:
        """批量检测
        
        Args:
            frames: 输入图像列表
            **kwargs: 检测参数
            
        Returns:
            原始模型输出列表
        """
        pass
    
    def is_loaded(self) -> bool:
        """检查模型是否已加载"""
        return hasattr(self, 'model') and self.model is not None


class YOLOStrategy(DetectionStrategy):
    """YOLO 检测策略（基于 ultralytics）
    
    支持 YOLOv8、YOLOv10 等 ultralytics 框架的模型
    """
    
    def __init__(self):
        self.model = None
        self.class_names = {}
    
    def load_model(self, model_path: str, **kwargs) -> None:
        """加载 YOLO 模型
        
        Args:
            model_path: .pt 模型文件路径
            **kwargs: 额外参数（如 device="cuda:0"）
        """
        try:
            from ultralytics import YOLO
            
            model_path_obj = Path(model_path)
            if not model_path_obj.exists():
                raise FileNotFoundError(f"模型文件不存在: {model_path}")
            
            logger.info(f"[YOLOStrategy] Loading model: {model_path}")
            self.model = YOLO(model_path)
            
            # 获取类别名称
            if hasattr(self.model, "names"):
                self.class_names = self.model.names
            
            logger.info(f"[YOLOStrategy] Model loaded successfully. Classes: {len(self.class_names)}")
            
        except ImportError:
            logger.error("[YOLOStrategy] ultralytics library not installed")
            raise RuntimeError("Please install ultralytics: pip install ultralytics")
        except Exception as e:
            logger.error(f"[YOLOStrategy] Model loading failed: {e}", exc_info=True)
            raise
    
    def detect(self, frame: np.ndarray, conf: float = 0.5, iou: float = 0.45, **kwargs) -> Any:
        """YOLO 单帧检测
        
        Args:
            frame: 输入图像
            conf: 置信度阈值
            iou: IOU 阈值
            **kwargs: 其他 YOLO predict 参数
            
        Returns:
            YOLO Results 对象
        """
        if not self.is_loaded():
            raise RuntimeError("Model not loaded. Call load_model() first.")
        
        results = self.model.predict(frame, conf=conf, iou=iou, verbose=False, **kwargs)
        return results
    
    def detect_batch(self, frames: List[np.ndarray], conf: float = 0.5, iou: float = 0.45, **kwargs) -> List[Any]:
        """YOLO 批量检测
        
        Args:
            frames: 输入图像列表
            conf: 置信度阈值
            iou: IOU 阈值
            **kwargs: 其他 YOLO predict 参数
            
        Returns:
            YOLO Results 对象列表
        """
        if not self.is_loaded():
            raise RuntimeError("Model not loaded. Call load_model() first.")
        
        results = self.model.predict(frames, conf=conf, iou=iou, verbose=False, **kwargs)
        return results


class TransformerStrategy(DetectionStrategy):
    """Transformer 检测策略（预留）
    
    未来可支持 DETR、DINO 等基于 Transformer 的检测模型
    """
    
    def __init__(self):
        self.model = None
    
    def load_model(self, model_path: str, **kwargs) -> None:
        """加载 Transformer 模型"""
        # TODO: 实现 Transformer 模型加载逻辑
        raise NotImplementedError("TransformerStrategy not implemented yet")
    
    def detect(self, frame: np.ndarray, **kwargs) -> Any:
        """Transformer 单帧检测"""
        raise NotImplementedError("TransformerStrategy not implemented yet")
    
    def detect_batch(self, frames: List[np.ndarray], **kwargs) -> List[Any]:
        """Transformer 批量检测"""
        raise NotImplementedError("TransformerStrategy not implemented yet")


# 策略工厂（可选）
class StrategyFactory:
    """策略工厂 - 根据类型创建策略实例"""
    
    _strategies = {
        "yolo": YOLOStrategy,
        "transformer": TransformerStrategy,
    }
    
    @classmethod
    def create(cls, strategy_type: str) -> DetectionStrategy:
        """创建策略实例
        
        Args:
            strategy_type: 策略类型 ("yolo", "transformer")
            
        Returns:
            DetectionStrategy 实例
        """
        strategy_cls = cls._strategies.get(strategy_type.lower())
        if strategy_cls is None:
            raise ValueError(f"Unknown strategy type: {strategy_type}. Available: {list(cls._strategies.keys())}")
        
        return strategy_cls()
    
    @classmethod
    def register(cls, name: str, strategy_cls: type):
        """注册新策略类型"""
        cls._strategies[name.lower()] = strategy_cls
