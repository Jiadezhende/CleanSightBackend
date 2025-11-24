"""
示例：如何创建自定义推理任务并集成到系统中

这个文件展示了如何扩展新的检测任务。
"""

import cv2
import numpy as np
from typing import Dict, Any, List

from app.services.ai import InferenceTask, InferenceResult


class BubbleDetectionTask(InferenceTask):
    """气泡检测任务示例（独立任务，不依赖其他任务）"""
    
    def __init__(self):
        super().__init__(name="bubble_detection", enabled=True)
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行气泡检测推理"""
        try:
            # TODO: 实现实际的气泡检测算法
            # 这里是模拟实现
            
            # 示例：简单的圆形检测作为气泡检测的占位符
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (9, 9), 2)
            
            # 检测圆形（模拟气泡）
            circles = cv2.HoughCircles(
                gray,
                cv2.HOUGH_GRADIENT,
                dp=1,
                minDist=50,
                param1=100,
                param2=30,
                minRadius=10,
                maxRadius=50
            )
            
            bubbles = []
            if circles is not None:
                circles = np.uint16(np.around(circles))
                for circle in circles[0, :]:
                    bubbles.append({
                        "center": (int(circle[0]), int(circle[1])),
                        "radius": int(circle[2])
                    })
            
            return {
                "success": True,
                "bubble_count": len(bubbles),
                "bubbles": bubbles,
                "detected": len(bubbles) > 0
            }
        except Exception as e:
            print(f"Bubble detection error: {e}")
            return {
                "success": False,
                "error": str(e),
                "bubble_count": 0,
                "bubbles": [],
                "detected": False
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """可视化气泡检测结果"""
        if not result.get("success"):
            return frame
        
        result_frame = frame.copy()
        bubbles = result.get("bubbles", [])
        
        # 绘制检测到的气泡
        for bubble in bubbles:
            center = bubble["center"]
            radius = bubble["radius"]
            cv2.circle(result_frame, center, radius, (255, 0, 255), 2)
            cv2.circle(result_frame, center, 2, (255, 0, 255), 3)
        
        # 显示气泡计数
        bubble_count = result.get("bubble_count", 0)
        if bubble_count > 0:
            cv2.putText(
                result_frame,
                f"Bubbles: {bubble_count}",
                (10, 150),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 0, 255),
                2
            )
        
        return result_frame


class CleanlinessTask(InferenceTask):
    """清洁度评估任务（依赖检测结果和气泡检测结果）"""
    
    def __init__(self):
        super().__init__(name="cleanliness", enabled=True)
    
    def requires_context(self) -> List[str]:
        """依赖检测任务和气泡检测任务"""
        return ["detection", "bubble_detection"]
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行清洁度评估"""
        try:
            results = context.get("results", {})
            
            # 获取检测结果
            detection_result = results.get("detection", {})
            bubble_result = results.get("bubble_detection", {})
            
            # 简单的清洁度评分算法（示例）
            score = 100.0
            
            # 根据气泡数量降低分数
            bubble_count = bubble_result.get("bubble_count", 0)
            score -= bubble_count * 5
            
            # TODO: 根据其他因素调整分数
            # - 污渍检测
            # - 内窥镜管道的清洁程度
            # - 等等
            
            score = max(0.0, min(100.0, score))
            
            # 确定清洁度等级
            if score >= 90:
                grade = "Excellent"
                color = (0, 255, 0)
            elif score >= 70:
                grade = "Good"
                color = (0, 255, 255)
            elif score >= 50:
                grade = "Fair"
                color = (0, 165, 255)
            else:
                grade = "Poor"
                color = (0, 0, 255)
            
            return {
                "success": True,
                "score": score,
                "grade": grade,
                "color": color,
                "factors": {
                    "bubble_count": bubble_count
                }
            }
        except Exception as e:
            print(f"Cleanliness evaluation error: {e}")
            return {
                "success": False,
                "error": str(e),
                "score": 0.0,
                "grade": "Unknown"
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """可视化清洁度评估结果"""
        if not result.get("success"):
            return frame
        
        result_frame = frame.copy()
        h, w = result_frame.shape[:2]
        
        score = result.get("score", 0.0)
        grade = result.get("grade", "Unknown")
        color = result.get("color", (255, 255, 255))
        
        # 在右上角显示清洁度信息
        text = f"Cleanliness: {grade} ({score:.1f})"
        cv2.putText(
            result_frame,
            text,
            (w - 300, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2
        )
        
        # 绘制进度条
        bar_width = 200
        bar_height = 20
        bar_x = w - 220
        bar_y = 50
        
        # 背景
        cv2.rectangle(
            result_frame,
            (bar_x, bar_y),
            (bar_x + bar_width, bar_y + bar_height),
            (100, 100, 100),
            -1
        )
        
        # 进度
        progress_width = int(bar_width * score / 100)
        cv2.rectangle(
            result_frame,
            (bar_x, bar_y),
            (bar_x + progress_width, bar_y + bar_height),
            color,
            -1
        )
        
        # 边框
        cv2.rectangle(
            result_frame,
            (bar_x, bar_y),
            (bar_x + bar_width, bar_y + bar_height),
            (255, 255, 255),
            2
        )
        
        return result_frame


# ============================================
# 如何使用这些自定义任务
# ============================================

def register_custom_tasks(manager):
    """
    将自定义任务注册到推理管理器
    
    使用方法:
    from app.services.ai import manager
    from app.services.example_custom_task import register_custom_tasks
    
    register_custom_tasks(manager)
    manager.start()
    """
    # 注册气泡检测任务
    manager.register_task(BubbleDetectionTask())
    
    # 注册清洁度评估任务
    manager.register_task(CleanlinessTask())
    
    print("Custom tasks registered successfully!")


def enable_disable_tasks_example(manager):
    """
    示例：动态启用/禁用任务
    """
    # 禁用某个任务
    manager.enable_task("bubble_detection", enabled=False)
    
    # 重新启用任务
    manager.enable_task("bubble_detection", enabled=True)
