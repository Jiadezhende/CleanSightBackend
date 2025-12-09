import asyncio
import json
import base64
import cv2
import numpy as np
import websockets
import argparse
from datetime import datetime, timedelta


class InferenceViewer:
    def __init__(self, client_id, show_window=True):
        self.client_id = client_id
        self.frame_count = 0
        self.start_time = None
        self.last_print_time = 0
        self.show_window = show_window
        self.window_name = f"AI推理结果 - {client_id}"
    
    async def connect_and_display(self, duration_seconds=None):
        ws_url = f"ws://localhost:8000/ai/video?client_id={self.client_id}"
        print(f"🔗 连接到 WebSocket: {ws_url}")
        
        self.start_time = datetime.now()
        
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
        
        # 处理Base64图像
        if message.startswith('data:image') and self.show_window:
            try:
                base64_data = message.split(',')[1]
                img_data = base64.b64decode(base64_data)
                img_array = np.frombuffer(img_data, dtype=np.uint8)
                frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                
                if frame is not None:
                    cv2.imshow(self.window_name, frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord('q'):
                        return False
            
            except Exception as e:
                print(f"⚠️ 图像解码失败: {e}")
        
        # 每秒打印状态
        current_time = datetime.now().timestamp()
        if current_time - self.last_print_time >= 1.0:
            elapsed = (datetime.now() - self.start_time).total_seconds()
            fps = self.frame_count / max(elapsed, 1)
            print(f"帧数: {self.frame_count} | FPS: {fps:.1f}")
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
