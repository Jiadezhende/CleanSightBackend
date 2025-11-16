import cv2
import queue
import time
import threading
from typing import Optional


def infer(frame):
    """
    Mock推理函数：给图像画一个矩形作为示例
    可替换为真实模型（YOLO、姿态估计等）
    """
    processed = frame.copy()
    h, w = processed.shape[:2]
    cx, cy = w // 2, h // 2
    rw, rh = min(200, w // 4), min(200, h // 4)
    x1 = max(0, cx - rw // 2)
    y1 = max(0, cy - rh // 2)
    x2 = min(w, cx + rw // 2)
    y2 = min(h, cy + rh // 2)

    # 画矩形与标签
    cv2.rectangle(processed, (x1, y1), (x2, y2), (0, 0, 255), 2)
    cv2.putText(processed, 'MockDet', (x1, max(10, y1-10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,0,255), 2)

    return processed


def inference_loop(frame_queue: queue.Queue, result_queue: queue.Queue, stop_event: Optional[threading.Event]=None):
    """
    推理循环（线程B）
    - 从 frame_queue 获取最新帧进行推理
    - 将推理结果放入 result_queue；若 result_queue 满则丢弃旧结果
    - 保证推理过程不会阻塞采集与推送
    """
    print("AI 推理服务已启动")
    while True:
        if stop_event is not None and stop_event.is_set():
            break

        try:
            # 尽量获取最新帧；阻塞等待 1s
            frame = frame_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        # 执行推理（这里为快速的 mock 推理）
        processed = infer(frame)

        # 结果队列只保留最新结果
        if result_queue.full():
            try:
                result_queue.get_nowait()
            except queue.Empty:
                pass

        try:
            result_queue.put_nowait(processed)
        except queue.Full:
            pass

        # 根据模型推理耗时调整休眠，示例中不强制休眠
        time.sleep(0.001)

    print("AI 推理服务已停止")