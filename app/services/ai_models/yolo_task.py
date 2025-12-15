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
            
            # 执行检测（仅获取检测结果，不绘制）
            detections, bending_detected = self.detector.detect(
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
                "bending_detected": False,
                "detections": [],
                "detection_count": 0,
                "bending_count": 0
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """
        可视化内镜弯折检测结果（在此处绘制检测框和状态信息）
        
        Args:
            frame: 输入图像
            result: 检测结果
            
        Returns:
            可视化后的图像
        """
        if not result.get("success"):
            return frame
        
        result_frame = frame.copy()
        
        # 获取检测结果
        detections = result.get("detections", [])
        bending_detected = result.get("bending_detected", False)
        bending_count = result.get("bending_count", 0)
        
        # 颜色定义（BGR格式）
        BENDING_COLOR = (0, 0, 255)   # 红色（弯折）
        NORMAL_COLOR = (0, 255, 0)    # 绿色（正常）
        TEXT_BG_COLOR = (0, 0, 0)     # 黑色
        TEXT_COLOR = (255, 255, 255)  # 白色
        
        # 绘制所有检测框
        for detection in detections:
            x1, y1, x2, y2 = detection["bbox"]
            conf = detection["confidence"]
            class_name = detection["class_name"]
            
            # 根据是否弯折选择颜色
            is_bending = "bent" in class_name.lower() or "bending" in class_name.lower()
            box_color = BENDING_COLOR if is_bending else NORMAL_COLOR
            
            # 绘制边界框
            cv2.rectangle(result_frame, (x1, y1), (x2, y2), box_color, 2)
            
            # 绘制标签
            label = f"{class_name} {conf:.2f}"
            (label_w, label_h), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            label_y = max(y1 - 10, label_h + 5)
            
            # 标签背景（带 padding）
            cv2.rectangle(
                result_frame,
                (x1, label_y - label_h - 5),
                (x1 + label_w + 6, label_y + 2),
                box_color,
                -1
            )
            
            # 标签文字（白色更醒目）
            cv2.putText(
                result_frame,
                label,
                (x1 + 3, label_y - 3),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                TEXT_COLOR,
                1,
                cv2.LINE_AA  # 抗锯齿
            )
        
        # 在左上角显示弯折状态（简化逻辑）
        if bending_detected:
            status_text = f"BENDING! Count: {bending_count}"
            status_color = BENDING_COLOR
        else:
            status_text = f"Normal (Count: {bending_count})"
            status_color = NORMAL_COLOR
        
        # 绘制状态栏（左上角，使用高效方法）
        self._draw_status_bar(result_frame, status_text, status_color, position="top-left")
        
        return result_frame
    
    def _draw_status_bar(self, frame: np.ndarray, text: str, color: tuple, position: str = "top-left"):
        """
        绘制状态栏的辅助方法
        
        Args:
            frame: 图像帧
            text: 状态文本
            color: 文本颜色
            position: 位置 ("top-right", "top-left", "bottom-left", "bottom-right")
        """
        height, width = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        thickness = 2
        padding = 10
        
        # 获取文本尺寸
        (text_w, text_h), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        
        # 根据位置计算坐标
        if position == "top-right":
            x = width - text_w - padding
            y = padding + text_h
        elif position == "top-left":
            x = padding
            y = padding + text_h
        elif position == "bottom-right":
            x = width - text_w - padding
            y = height - padding
        else:  # bottom-left
            x = padding
            y = height - padding
        
        # 绘制背景矩形（不透明，性能更好）
        bg_padding = 5
        cv2.rectangle(
            frame,
            (x - bg_padding, y - text_h - bg_padding),
            (x + text_w + bg_padding, y + bg_padding),
            (0, 0, 0),
            -1
        )
        
        # 绘制文本
        cv2.putText(
            frame,
            text,
            (x, y),
            font,
            font_scale,
            color,
            thickness,
            cv2.LINE_AA
        )
    
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
