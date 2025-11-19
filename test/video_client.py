import argparse
import asyncio
import websockets
import cv2
import base64
import numpy as np
import time


async def display_video(client_id: str = "client_local", server_ws: str = "ws://127.0.0.1:8000"):
    uri = f"{server_ws}/ai/video?client_id={client_id}"
    async with websockets.connect(uri) as websocket:  # 设置超时时间为10秒
        while True:
            try:
                # 接收 Base64 编码的 JPEG 图像字符串
                data_url = str(await asyncio.wait_for(websocket.recv(), timeout=5))

                # 提取 Base64 数据部分（去掉 "data:image/jpeg;base64," 前缀）
                base64_data = data_url.split(",")[1]

                # 解码 Base64 字符串为字节数据
                img_data = base64.b64decode(base64_data)

                # 将字节数据转换为 NumPy 数组
                np_arr = np.frombuffer(img_data, np.uint8)

                # 解码为 OpenCV 图像
                frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

                # 检查解码是否成功
                if frame is None:
                    print("Failed to decode image")
                    continue

                # 自适应窗口大小：缩放到最大边不超过 800 像素
                height, width = frame.shape[:2]
                max_size = 800
                if width > max_size or height > max_size:
                    scale = min(max_size / width, max_size / height)
                    new_width = int(width * scale)
                    new_height = int(height * scale)
                    frame = cv2.resize(frame, (new_width, new_height))

                # 显示图像
                cv2.imshow("Inference Result", frame)

                # 按下 'q' 键退出
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            except asyncio.TimeoutError:
                print("接收数据超时")
                break
            except Exception as e:
                print(f"Error: {e}")
                break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="显示客户端（接收 AI 推理结果）")
    parser.add_argument('--client-id', default='client_local', help='客户端 ID，用于订阅对应结果')
    parser.add_argument('--server-ws', default='ws://127.0.0.1:8000', help='服务 WS 地址')
    args = parser.parse_args()

    try:
        asyncio.run(display_video(client_id=args.client_id, server_ws=args.server_ws))
    except KeyboardInterrupt:
        print('已中断')