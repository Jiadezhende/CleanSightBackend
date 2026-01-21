"""
气泡检测任务

使用 YOLO 模型检测内镜清洗过程中的气泡，并更新清洗任务的气泡计数
"""

import cv2
import numpy as np
from typing import Dict, Any, List, Optional

from app.services.infer_task import InferenceTask, InferenceResult
from app.services.ai_models.bubble_detection import get_bubble_detector


class BubbleDetectionTask(InferenceTask):
    """气泡检测任务"""
    
    def __init__(
        self, 
        model_path: Optional[str] = None,
        conf_threshold: Optional[float] = None,
        iou_threshold: Optional[float] = None,
        enabled: bool = True
    ):
        """
        初始化气泡检测任务
        
        Args:
            model_path: YOLO 模型路径，如果为 None 则从配置读取
            conf_threshold: 置信度阈值 (0.0-1.0)，如果为 None 则使用默认值 0.25
            iou_threshold: IOU 阈值 (0.0-1.0)，如果为 None 则使用默认值 0.45
            enabled: 是否启用此任务
        """
        super().__init__(name="bubble_detection", enabled=enabled)
        
        # 从配置读取默认值
        if model_path is None:
            from app.settings import settings
            # 尝试使用专门的气泡检测模型配置，否则使用通用 YOLO 配置
            model_path = getattr(settings, 'bubble_model_path', settings.yolo_model_path)
        
        # 使用气泡检测的默认阈值
        if conf_threshold is None:
            conf_threshold = 0.25  # 气泡检测置信度阈值
        if iou_threshold is None:
            iou_threshold = 0.45  # NMS IOU 阈值
        
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
                self.detector = get_bubble_detector(self.model_path)
                self._model_loaded = True
                print(f"气泡检测模型已加载: {self.model_path}")
            except Exception as e:
                print(f"气泡检测模型加载失败: {e}")
                raise
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """
        执行气泡检测
        
        Args:
            frame: 输入图像
            context: 上下文信息，包含清洗任务对象
            
        Returns:
            检测结果，包含是否检测到气泡、检测框、气泡数量等信息
        """
        try:
            # 确保模型已加载
            self._ensure_model_loaded()
            
            # 执行检测（仅获取检测结果，不绘制）
            detections, bubble_detected, bubble_count = self.detector.detect(
                frame,
                conf_threshold=self.conf_threshold,
                iou_threshold=self.iou_threshold
            )
            
            # 获取清洗任务对象
            task = context.get("task")
            
            # 如果检测到气泡且有任务对象，更新气泡计数
            # 注意：这里可以根据业务需求决定是累计总气泡数还是记录帧数
            if bubble_detected and task:
                # 这里假设任务对象有 bubble_count 属性
                # 如果没有，需要在 Task 模型中添加此字段
                if not hasattr(task, 'bubble_count'):
                    task.bubble_count = 0
                task.bubble_count += bubble_count
                print(f"检测到 {bubble_count} 个气泡！任务 {task.task_id} 气泡总计: {task.bubble_count}")
            
            return {
                "success": True,
                "bubble_detected": bubble_detected,
                "detections": detections,
                "bubble_count": bubble_count,
                "total_bubble_count": task.bubble_count if task and hasattr(task, 'bubble_count') else 0
            }
            
        except Exception as e:
            print(f"气泡检测错误: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e),
                "bubble_detected": False,
                "detections": [],
                "bubble_count": 0,
                "total_bubble_count": 0
            }

    def infer_batch(self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]) -> List[InferenceResult]:
        """使用 detector 的批量接口进行气泡批量检测，返回每帧结果列表。"""
        try:
            self._ensure_model_loaded()
            batch_results = self.detector.detect_batch(frames, conf_threshold=self.conf_threshold, iou_threshold=self.iou_threshold)
            out: List[InferenceResult] = []
            for (detections, bubble_detected, bubble_count), ctx in zip(batch_results, contexts):
                task = ctx.get('task')
                if bubble_detected and task:
                    if not hasattr(task, 'bubble_count'):
                        task.bubble_count = 0
                    task.bubble_count += bubble_count
                out.append({
                    "success": True,
                    "bubble_detected": bubble_detected,
                    "detections": detections,
                    "bubble_count": bubble_count,
                    "total_bubble_count": task.bubble_count if task and hasattr(task, 'bubble_count') else 0
                })
            return out
        except Exception as e:
            print(f"气泡批量检测错误: {e}")
            import traceback
            traceback.print_exc()
            return [self.infer(f, c) for f, c in zip(frames, contexts)]
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """
        可视化气泡检测结果（在此处绘制检测框和状态信息）
        
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
        bubble_detected = result.get("bubble_detected", False)
        bubble_count = result.get("bubble_count", 0)
        total_bubble_count = result.get("total_bubble_count", 0)
        
        # 颜色定义（BGR格式）
        BUBBLE_COLOR = (0, 255, 255)      # 正常气泡检测框：黄色
        DEBUG_BUBBLE_COLOR = (255, 0, 255)  # 调试框：洋红色，便于与弯折区分
        NORMAL_COLOR = (0, 255, 0)        # 绿色
        WARNING_COLOR = (0, 165, 255)     # 橙色
        TEXT_BG_COLOR = (0, 0, 0)         # 黑色
        
        # 绘制所有检测框
        for detection in detections:
            x1, y1, x2, y2 = detection["bbox"]
            conf = detection["confidence"]
            class_name = detection["class_name"]

            # 调试框单独使用 DEBUG_BUBBLE_COLOR
            box_color = DEBUG_BUBBLE_COLOR if class_name == "bubble_debug_box" else BUBBLE_COLOR

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
            
            # 标签文字（黑色更清晰）
            cv2.putText(
                result_frame,
                label,
                (x1 + 3, label_y - 3),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                TEXT_BG_COLOR,
                1,
                cv2.LINE_AA  # 抗锯齿
            )
        
        # 在右上角显示气泡状态（简化信息）
        if bubble_detected:
            status_text = f"Bubbles: {bubble_count} (Total: {total_bubble_count})"
            status_color = WARNING_COLOR if bubble_count > 5 else BUBBLE_COLOR
        else:
            status_text = f"No Bubbles (Total: {total_bubble_count})"
            status_color = NORMAL_COLOR
        
        # 绘制状态栏（右上角，使用高效方法）
        self._draw_status_bar(result_frame, status_text, status_color, position="top-right")
        
        return result_frame
    
    def _draw_status_bar(self, frame: np.ndarray, text: str, color: tuple, position: str = "top-right"):
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
        """气泡检测是独立任务，不依赖其他任务"""
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
        
        print(f"气泡检测阈值已更新: conf={self.conf_threshold}, iou={self.iou_threshold}")
