import argparse
import asyncio
import base64
import json
from datetime import datetime, timedelta

import cv2
import numpy as np
import websockets


class InferenceViewer:
    def __init__(self, client_id, show_window=True, base_port="localhost:8000"):
        self.client_id = client_id
        self.frame_count = 0
        self.start_time = None
        self.last_print_time = 0
        self.last_second_frames = 0  # 上一秒的帧数计数器
        self.show_window = show_window
        self.window_name = f"AI Inference Result - {client_id}"
        self.base_port = base_port

    async def connect_and_display(self, duration_seconds=None):
        ws_url = f"ws://{self.base_port}/ai/video?client_id={self.client_id}"
        print(f"🔗 连接到 WebSocket: {ws_url}")

        self.start_time = datetime.now()
        self.last_print_time = datetime.now().timestamp()  # 初始化打印时间

        try:
            async with websockets.connect(ws_url) as websocket:
                print("✅ WebSocket 连接成功")

                end_time = None
                if duration_seconds:
                    end_time = datetime.now() + timedelta(seconds=duration_seconds)

                while True:
                    if end_time and datetime.now() > end_time:
                        break

                    message = await websocket.recv()
                    if not await self.process_message(message):
                        break

        except Exception as e:
            print(f"❌ WebSocket 错误: {e}")
        finally:
            if self.show_window:
                cv2.destroyAllWindows()

    async def process_message(self, message):
        self.frame_count += 1
        self.last_second_frames += 1  # 累计当前秒的帧数

        # 处理Base64图像
        if message.startswith("data:image") and self.show_window:
            try:
                base64_data = message.split(",")[1]
                img_data = base64.b64decode(base64_data)
                img_array = np.frombuffer(img_data, dtype=np.uint8)
                frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

                if frame is not None:
                    cv2.imshow(self.window_name, frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        return False

            except Exception as e:
                print(f"⚠️ 图像解码失败: {e}")

        # 每秒打印状态
        current_time = datetime.now().timestamp()
        if current_time - self.last_print_time >= 1.0:
            # 计算瞬时FPS（最近1秒的实际帧率）
            interval = current_time - self.last_print_time
            instant_fps = self.last_second_frames / interval

            # 计算平均FPS（从开始到现在）
            elapsed = (datetime.now() - self.start_time).total_seconds()
            avg_fps = self.frame_count / max(elapsed, 1)

            print(
                f"📊 总帧数: {self.frame_count} | 瞬时FPS: {instant_fps:.1f} | 平均FPS: {avg_fps:.1f}"
            )

            # 重置计数器
            self.last_second_frames = 0
            self.last_print_time = current_time

        return True


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client_id", default="test_client")
    parser.add_argument("--duration", type=int, default=None)
    parser.add_argument("--no-window", action="store_true")

    args = parser.parse_args()

    viewer = InferenceViewer(args.client_id, show_window=not args.no_window)
    await viewer.connect_and_display(args.duration)


if __name__ == "__main__":
    asyncio.run(main())
