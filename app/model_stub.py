import cv2
import numpy as np

def infer(frame):
    """
    Mock推理函数：给图像画一个矩形作为示例
    实际使用时可替换为YOLO、姿态估计等模型

    Args:
        frame: 输入图像帧 (numpy array)

    Returns:
        处理后的图像帧 (numpy array)
    """
    # 创建图像副本
    processed_frame = frame.copy()

    # 获取图像尺寸
    height, width = processed_frame.shape[:2]

    # 在图像中心画一个矩形 (模拟检测结果)
    center_x, center_y = width // 2, height // 2
    rect_width, rect_height = 100, 100

    # 矩形坐标
    x1 = max(0, center_x - rect_width // 2)
    y1 = max(0, center_y - rect_height // 2)
    x2 = min(width, center_x + rect_width // 2)
    y2 = min(height, center_y + rect_height // 2)

    # 画矩形 (红色边框)
    cv2.rectangle(processed_frame, (x1, y1), (x2, y2), (0, 0, 255), 2)

    # 添加文本标签
    cv2.putText(processed_frame, "Detected", (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

    return processed_frame