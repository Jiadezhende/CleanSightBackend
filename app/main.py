import cv2
import base64
import threading
import queue
import asyncio
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from app.model_stub import infer

# 创建双队列：frame_queue用于存储最新帧，result_queue用于存储推理结果
frame_queue = queue.Queue(maxsize=1)  # 只保留最新帧
result_queue = queue.Queue(maxsize=1)  # 只保留最新推理结果

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动后台线程
    start_background_threads()
    yield
    # 清理资源（如果需要）

app = FastAPI(
    title="CleanSight Backend",
    description="AI-powered inspection of the endoscope cleaning process at Changhai Hospital",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/")
async def root():
    return {"message": "Welcome to CleanSight Backend"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

def capture_frames():
    """
    线程A：摄像头采集线程
    从摄像头或本地视频读取帧，只保留最新帧到frame_queue
    实现跳帧机制：如果队列满，丢弃旧帧
    """
    # 尝试打开摄像头，如果失败则使用本地视频文件
    cap = cv2.VideoCapture(0)  # 0表示默认摄像头
    if not cap.isOpened():
        print("无法打开摄像头，尝试使用本地视频文件...")
        cap = cv2.VideoCapture("demo.mp4")  # 本地视频文件
        if not cap.isOpened():
            print("无法打开视频文件，请确保demo.mp4存在或摄像头可用")
            return

    print("开始采集视频帧...")
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("视频流结束或读取失败")
                break

            # 跳帧机制：只保留最新帧
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()  # 丢弃旧帧
                except queue.Empty:
                    pass
            frame_queue.put(frame, timeout=0.1)  # 非阻塞放入

            time.sleep(0.03)  # 控制采集频率，约30FPS
    finally:
        cap.release()
        print("摄像头采集线程结束")

def process_frames():
    """
    线程B：模型推理线程
    从frame_queue取最新帧，进行推理处理，放入result_queue
    实现跳帧机制：如果结果队列满，丢弃旧结果
    """
    print("开始模型推理处理...")
    while True:
        try:
            # 从队列获取帧（阻塞等待）
            frame = frame_queue.get(timeout=1.0)

            # 执行推理
            processed_frame = infer(frame)

            # 放入结果队列（跳帧机制）
            if result_queue.full():
                try:
                    result_queue.get_nowait()  # 丢弃旧结果
                except queue.Empty:
                    pass
            result_queue.put(processed_frame, timeout=0.1)  # 非阻塞放入

        except queue.Empty:
            continue  # 队列为空，继续等待

@app.websocket("/ws/video")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket端点：/ws/video
    线程C：推送线程，从result_queue推送Base64编码的JPEG图像给前端
    """
    await websocket.accept()
    print(f"WebSocket连接已建立: {websocket.client}")

    try:
        while True:
            try:
                # 从结果队列获取处理后的帧（非阻塞）
                processed_frame = result_queue.get_nowait()

                # 编码为JPEG
                _, buffer = cv2.imencode('.jpg', processed_frame)
                jpg_as_text = base64.b64encode(buffer.tobytes()).decode('utf-8')

                # 格式化为data URL
                data_url = f"data:image/jpeg;base64,{jpg_as_text}"

                # 发送给前端
                await websocket.send_text(data_url)

            except queue.Empty:
                pass  # 队列为空，继续循环

            # 控制发送频率，避免占用过多带宽
            await asyncio.sleep(0.03)  # 约30FPS

    except Exception as e:
        print(f"WebSocket错误: {e}")
    finally:
        print(f"WebSocket连接已关闭: {websocket.client}")

# 在应用启动时启动后台线程
def start_background_threads():
    """启动采集和推理线程"""
    capture_thread = threading.Thread(target=capture_frames, daemon=True, name="CaptureThread")
    inference_thread = threading.Thread(target=process_frames, daemon=True, name="InferenceThread")

    capture_thread.start()
    inference_thread.start()

    print("后台线程已启动：采集线程和推理线程")