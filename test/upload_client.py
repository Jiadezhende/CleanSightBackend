import requests
import base64
import cv2
import time
import argparse
import os
import asyncio
import websockets
import uuid

# 读取本地图像（或视频帧）
def get_base64_frame(image_path="test_frame.jpg"):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")

# 模拟上传帧
def upload_frame(frame_data, server_http="http://127.0.0.1:8000", client_id="client_local"):
    url = f"{server_http}/inspection/upload_frame"
    data = {"client_id": client_id, "frame": frame_data}
    try:
        response = requests.post(url, data=data, timeout=5)
        try:
            print("Upload response:", response.json())
        except Exception:
            print("Upload response (text):", response.text)
    except Exception as e:
        print(f"Upload error: {e}")

# 模式1: 上传静态帧
def upload_static_frame(server_http="http://127.0.0.1:8000", client_id="client_local"):
    frame_data = get_base64_frame()
    while True:
        upload_frame(frame_data, server_http=server_http, client_id=client_id)
        time.sleep(0.01)  # 每 0.01 秒上传一次，尽可能快

# 模式2: 上传视频（传完自动停止）
def upload_video(video_path="test_video.mp4", server_http="http://127.0.0.1:8000", client_id="client_local"):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"无法打开视频文件: {video_path}")
        return

    # 获取视频帧率（用于参考，但现在全速上传以减少限制）
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps > 0:
        print(f"视频帧率: {fps} FPS，现在全速上传")
    else:
        print("无法获取帧率，全速上传")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("视频上传完成")
                break

            # 编码为 Base64
            success, buffer = cv2.imencode('.jpg', frame)
            if success:
                frame_data = base64.b64encode(buffer.tobytes()).decode("utf-8")
                upload_frame(frame_data, server_http=server_http, client_id=client_id)
    finally:
        cap.release()

# 模式3: 调取本地摄像头
def upload_camera(source=0, server_http="http://127.0.0.1:8000", client_id="client_local"):
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print("无法打开摄像头")
        return

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("摄像头读取失败")
                break

            # 编码为 Base64
            success, buffer = cv2.imencode('.jpg', frame)
            if success:
                frame_data = base64.b64encode(buffer.tobytes()).decode("utf-8")
                upload_frame(frame_data, server_http=server_http, client_id=client_id)

            # 移除 sleep，全速上传
    finally:
        cap.release()

# 模式4: WebSocket 实时上传摄像头
async def websocket_upload_camera(source=0, server_ws="ws://127.0.0.1:8000", client_id="client_local"):
    uri = f"{server_ws}/inspection/upload_stream?client_id={client_id}"
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"无法打开摄像头: {source}")
        return

    try:
        async with websockets.connect(uri) as websocket:
            print("WebSocket 连接已建立，开始上传摄像头帧")
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("摄像头读取失败")
                    break

                # 编码为 Base64
                success, buffer = cv2.imencode('.jpg', frame)
                if success:
                    frame_data = base64.b64encode(buffer.tobytes()).decode("utf-8")
                    
                    # 发送帧
                    await websocket.send(frame_data)
                    
                    # 接收确认
                    response = await websocket.recv()
                    if response != "success":
                        print(f"上传失败: {response}")
                else:
                    print("帧编码失败")
                    
                # 控制帧率，避免过快
                await asyncio.sleep(0.03)  # ~30 FPS
                
    except Exception as e:
        print(f"WebSocket 错误: {e}")
    finally:
        cap.release()

# 模式5: WebSocket 上传视频
async def websocket_upload_video(video_path="test_video.mp4", server_ws="ws://127.0.0.1:8000", client_id="client_local"):
    uri = f"{server_ws}/inspection/upload_stream?client_id={client_id}"
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"无法打开视频文件: {video_path}")
        return

    # 获取视频帧率（用于参考，但现在全速上传以减少限制）
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps > 0:
        print(f"视频帧率: {fps} FPS，现在全速上传")
    else:
        print("无法获取帧率，全速上传")

    try:
        async with websockets.connect(uri) as websocket:
            print("WebSocket 连接已建立，开始上传视频帧")
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("视频上传完成")
                    break

                # 编码为 Base64
                success, buffer = cv2.imencode('.jpg', frame)
                if success:
                    frame_data = base64.b64encode(buffer.tobytes()).decode("utf-8")
                    
                    # 发送帧
                    await websocket.send(frame_data)
                    
                    # 接收确认
                    response = await websocket.recv()
                    if response != "success":
                        print(f"上传失败: {response}")
                else:
                    print("帧编码失败")
                    
                # 控制帧率，避免过快，与摄像头一致 ~30 FPS
                await asyncio.sleep(0.03)
                
    except Exception as e:
        print(f"WebSocket 错误: {e}")
    finally:
        cap.release()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试客户端：选择上传模式")
    parser.add_argument("--mode", choices=["frame", "video", "camera"], default="frame",
                        help="选择模式: frame (静态帧), video (上传视频), camera (摄像头)")
    parser.add_argument("--transport", choices=["http", "websocket"], default="http",
                        help="选择传输方式: http (POST请求), websocket (WebSocket连接) - 适用于 video 和 camera 模式")
    parser.add_argument("--video", default="test_video.mp4", help="视频文件路径 (用于 video 模式)")
    parser.add_argument("--source", type=int, default=0, help="摄像头索引 (用于 camera 模式)")
    parser.add_argument("--client-id", default=f"client_{uuid.uuid4().hex[:6]}", help="客户端 ID")
    parser.add_argument("--server-http", default="http://127.0.0.1:8000", help="服务 HTTP 地址")
    parser.add_argument("--server-ws", default="ws://127.0.0.1:8000", help="服务 WS 地址")

    args = parser.parse_args()

    if args.mode == "frame":
        upload_static_frame(server_http=args.server_http, client_id=args.client_id)
    elif args.mode == "video":
        if args.transport == "http":
            upload_video(args.video, server_http=args.server_http, client_id=args.client_id)
        elif args.transport == "websocket":
            asyncio.run(websocket_upload_video(args.video, server_ws=args.server_ws, client_id=args.client_id))
    elif args.mode == "camera":
        if args.transport == "http":
            upload_camera(args.source, server_http=args.server_http, client_id=args.client_id)
        elif args.transport == "websocket":
            asyncio.run(websocket_upload_camera(args.source, server_ws=args.server_ws, client_id=args.client_id))