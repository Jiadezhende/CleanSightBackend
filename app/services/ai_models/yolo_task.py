"""
内镜弯折检测任务

使用 YOLO 模型检测内镜是否弯折，并更新清洗任务的弯折计数
"""

import cv2
import numpy as np
from typing import Dict, Any, List, Optional

from app.services.ai import InferenceTask, InferenceResult
from app.services.ai_models.yolo_detection import get_detector


class EndoscopeBendingDetectionTask(InferenceTask):
    """内镜弯折检测任务"""
    
    def __init__(
        self, 
        model_path: Optional[str] = None,
        conf_threshold: Optional[float] = None,
        iou_threshold: Optional[float] = None,
        enabled: bool = True
    ):
        """
        初始化内镜弯折检测任务
        
        Args:
            model_path: YOLO 模型路径，如果为 None 则从配置读取
            conf_threshold: 置信度阈值 (0.0-1.0)，如果为 None 则从配置读取
            iou_threshold: IOU 阈值 (0.0-1.0)，如果为 None 则从配置读取
            enabled: 是否启用此任务
        """
        super().__init__(name="endoscope_bending_detection", enabled=enabled)
        
        # 从配置读取默认值
        if model_path is None or conf_threshold is None or iou_threshold is None:
            from app.config import settings
            if model_path is None:
                model_path = settings.yolo_model_path
            if conf_threshold is None:
                conf_threshold = settings.yolo_conf_threshold
            if iou_threshold is None:
                iou_threshold = settings.yolo_iou_threshold
        
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.detector = None
        
        # 延迟加载模型（在第一次推理时加载）
        self._model_loaded = False
    
    def _ensure_model_loaded(self):
        """确保模型已加载"""
        if not self._model_loaded:
            try:
                self.detector = get_detector(self.model_path)
                self._model_loaded = True
                print(f"内镜弯折检测模型已加载: {self.model_path}")
            except Exception as e:
                print(f"内镜弯折检测模型加载失败: {e}")
                raise
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """
        执行内镜弯折检测
        
        Args:
            frame: 输入图像
            context: 上下文信息，包含清洗任务对象
            
        Returns:
            检测结果，包含是否检测到弯折、检测框等信息
        """
        try:
            # 确保模型已加载
            self._ensure_model_loaded()
            
            # 执行检测
            annotated_frame, detections, bending_detected = self.detector.detect(
                frame,
                conf_threshold=self.conf_threshold,
                iou_threshold=self.iou_threshold
            )
            
            # 获取清洗任务对象
            task = context.get("task")
            
            # 如果检测到弯折且有任务对象，更新弯折计数
            if bending_detected and task:
                # 更新任务的弯折计数
                task.bending_count += 1
                print(f"检测到内镜弯折！任务 {task.task_id} 弯折计数: {task.bending_count}")
            
            return {
                "success": True,
                "annotated_frame": annotated_frame,
                "bending_detected": bending_detected,
                "detections": detections,
                "detection_count": len(detections),
                "bending_count": task.bending_count if task else 0
            }
            
        except Exception as e:
            print(f"内镜弯折检测错误: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e),
                "annotated_frame": frame.copy(),
                "bending_detected": False,
                "detections": [],
                "detection_count": 0,
                "bending_count": 0
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """
        可视化内镜弯折检测结果
        
        Args:
            frame: 输入图像
            result: 检测结果
            
        Returns:
            可视化后的图像
        """
        if not result.get("success"):
            return frame
        
        # 使用已标注的帧（包含检测框）
        result_frame = result.get("annotated_frame", frame).copy()
        
        # 在左上角显示弯折状态
        bending_detected = result.get("bending_detected", False)
        bending_count = result.get("bending_count", 0)
        
        # 设置状态文本和颜色
        if bending_detected:
            status_text = f"BENDING DETECTED! Count: {bending_count}"
            color = (0, 0, 255)  # 红色警告
        else:
            status_text = f"Normal - Bending Count: {bending_count}"
            color = (0, 255, 0)  # 绿色正常
        
        # 绘制半透明背景
        overlay = result_frame.copy()
        cv2.rectangle(overlay, (5, 5), (450, 35), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.5, result_frame, 0.5, 0, result_frame)
        
        # 绘制状态文本
        cv2.putText(
            result_frame,
            status_text,
            (10, 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2
        )
        
        return result_frame
    
    def requires_context(self) -> List[str]:
        """内镜弯折检测是独立任务，不依赖其他任务"""
        return []
    
    def set_thresholds(self, conf_threshold: float = None, iou_threshold: float = None):
        """
        动态调整检测阈值
        
        Args:
            conf_threshold: 置信度阈值
            iou_threshold: IOU 阈值
        """
        if conf_threshold is not None:
            self.conf_threshold = max(0.0, min(1.0, conf_threshold))
        if iou_threshold is not None:
            self.iou_threshold = max(0.0, min(1.0, iou_threshold))
        
        print(f"内镜弯折检测阈值已更新: conf={self.conf_threshold}, iou={self.iou_threshold}")
