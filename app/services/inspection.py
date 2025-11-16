import os
import cv2
import time
import queue
import base64
import numpy as np

# local test only
def camera_capture_loop(frame_queue: queue.Queue, source=0, stop_event=None):
    """
    摄像头采集循环（线程A）
    - 优先尝试打开摄像头（device index），失败则尝试打开本地文件 `demo.mp4`
    - 只保留最新一帧：当 `frame_queue` 满时丢弃旧帧

    Args:
        frame_queue: queue.Queue(maxsize=1) 用于放置最新帧
        source: 摄像头索引或视频文件路径（默认0）
        stop_event: 可选的 threading.Event，用于优雅停止
    """
    # 优先尝试以数字索引打开摄像头
    cap = None

    # 获取当前文件所在目录的绝对路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    video_path = os.path.join(current_dir, "../../test/demo.mp4")
    
    try:
        # cap = cv2.VideoCapture(source)
        # if not cap.isOpened():
        #     # 尝试回退到本地文件 demo.mp4
        #     cap.release()
        #     cap = cv2.VideoCapture(video_path)
        #     if not cap.isOpened():
        #         print("无法打开摄像头或 demo.mp4 文件。请检查设备或将 demo.mp4 放到项目根目录。")
        #         return
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print("无法打开摄像头或 demo.mp4 文件。请检查设备或将 demo.mp4 放到项目根目录。")
            return
    except Exception as e:
        print(f"打开视频源时出错: {e}")
        return

    print("摄像头采集服务已启动")

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break

            ret, frame = cap.read()
            if not ret:
                # 视频播放结束，停止采集（模拟客户端停止上传）
                print("视频读取结束，停止采集")
                break

            # 若队列已满，丢弃旧帧，保证只保留最新帧
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()
                except queue.Empty:
                    pass

            try:
                frame_queue.put_nowait(frame)
            except queue.Full:
                # 若仍然满（极少发生），直接忽略本帧
                pass

            # 不要阻塞太久，保持实时性。调整为合适的采集间隔
            time.sleep(0.03)
    finally:
        cap.release()
        print("摄像头采集服务已停止")

# wireless camera frame handler
def handle_network_frame(frame_queue: queue.Queue, base64_frame: str):
    """
    处理网络上传的Base64编码视频帧，并加入队列。

    Args:
        frame_queue: queue.Queue(maxsize=1) 用于放置最新帧
        base64_frame: Base64编码的图像帧

    Returns:
        str: 状态信息
    """
    try:
        # 解码Base64图像
        frame_data = base64.b64decode(base64_frame)
        np_arr = np.frombuffer(frame_data, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        if img is None:
            return "error: 无效的图像数据"

        # 将解码后的图像加入队列
        if frame_queue.full():
            try:
                frame_queue.get_nowait()  # 丢弃旧帧
            except queue.Empty:
                pass

        try:
            frame_queue.put_nowait(img)
            return "success"
        except queue.Full:
            return "error: 队列已满，无法添加帧"

    except Exception as e:
        print(f"处理网络帧时出错: {e}")
        return f"error: {str(e)}"
