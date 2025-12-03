"""
摄像头采集客户端
功能：
1. 启动摄像头采集并通过WebSocket上传视频流到服务器
2. 停止摄像头和上传
"""

import asyncio
import base64
import cv2
import time
import websockets
from typing import Optional
import threading
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class CameraClient:
    """摄像头采集客户端"""
    
    def __init__(
        self,
        client_id: str,
        server_url: str = "ws://localhost:8000/inspection/upload_stream",
        camera_id: int = 0,
        fps: int = 30,
        jpeg_quality: int = 70,
        frame_width: int = 640,
        frame_height: int = 480
    ):
        """
        初始化摄像头客户端
        
        Args:
            client_id: 客户端唯一标识符
            server_url: WebSocket服务器地址
            camera_id: 摄像头ID (0为默认摄像头)
            fps: 采集帧率
            jpeg_quality: JPEG编码质量 (1-100)
            frame_width: 视频帧宽度
            frame_height: 视频帧高度
        """
        self.client_id = client_id
        self.server_url = server_url
        self.camera_id = camera_id
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.frame_width = frame_width
        self.frame_height = frame_height
        
        # 运行状态
        self.is_running = False
        self.camera: Optional[cv2.VideoCapture] = None
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.upload_task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        
        # 统计信息
        self.frames_sent = 0
        self.frames_success = 0
        self.frames_error = 0
        self.start_time = 0.0
        
    def _init_camera(self) -> bool:
        """
        初始化摄像头
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            self.camera = cv2.VideoCapture(self.camera_id)
            if not self.camera.isOpened():
                logger.error(f"❌ 无法打开摄像头 {self.camera_id}")
                return False
            
            # 设置摄像头参数
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
            self.camera.set(cv2.CAP_PROP_FPS, self.fps)
            
            # 读取实际参数
            actual_width = int(self.camera.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(self.camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = int(self.camera.get(cv2.CAP_PROP_FPS))
            
            logger.info(f"✅ 摄像头初始化成功")
            logger.info(f"   分辨率: {actual_width}x{actual_height}")
            logger.info(f"   帧率: {actual_fps} FPS")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ 摄像头初始化失败: {e}")
            return False
    
    def _encode_frame(self, frame) -> Optional[str]:
        """
        编码视频帧为Base64 JPEG格式
        
        Args:
            frame: OpenCV图像帧
            
        Returns:
            Base64编码的JPEG字符串，失败返回None
        """
        try:
            # JPEG编码
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
            _, buffer = cv2.imencode('.jpg', frame, encode_param)
            
            # Base64编码
            frame_b64 = base64.b64encode(buffer).decode('utf-8')
            return frame_b64
            
        except Exception as e:
            logger.error(f"❌ 帧编码失败: {e}")
            return None
    
    async def _upload_loop(self):
        """视频上传循环（异步）"""
        full_url = f"{self.server_url}?client_id={self.client_id}"
        
        try:
            logger.info(f"🔌 正在连接到服务器: {full_url}")
            
            async with websockets.connect(full_url) as websocket:
                self.websocket = websocket
                logger.info("✅ WebSocket连接成功")
                
                frame_interval = 1.0 / self.fps
                
                while self.is_running:
                    loop_start = time.time()
                    
                    # 读取摄像头帧
                    if self.camera is None or not self.camera.isOpened():
                        logger.error("❌ 摄像头未打开")
                        break
                    
                    ret, frame = self.camera.read()
                    if not ret:
                        logger.error("❌ 无法读取摄像头帧")
                        break
                    
                    # 编码帧
                    frame_b64 = self._encode_frame(frame)
                    if frame_b64 is None:
                        self.frames_error += 1
                        continue
                    
                    # 发送帧
                    try:
                        await websocket.send(frame_b64)
                        self.frames_sent += 1
                        
                        # 异步接收响应（不阻塞）
                        try:
                            response = await asyncio.wait_for(
                                websocket.recv(), 
                                timeout=0.001
                            )
                            if response == "success":
                                self.frames_success += 1
                            else:
                                self.frames_error += 1
                        except asyncio.TimeoutError:
                            # 响应超时，继续发送下一帧
                            pass
                            
                    except Exception as e:
                        logger.error(f"❌ 发送帧失败: {e}")
                        self.frames_error += 1
                    
                    # 控制帧率
                    elapsed = time.time() - loop_start
                    sleep_time = max(0, frame_interval - elapsed)
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)
                    
                    # 每30帧输出一次统计
                    if self.frames_sent > 0 and self.frames_sent % 30 == 0:
                        elapsed_total = time.time() - self.start_time
                        actual_fps = self.frames_sent / elapsed_total if elapsed_total > 0 else 0
                        logger.info(
                            f"📊 发送: {self.frames_sent} 帧 | "
                            f"成功: {self.frames_success} | "
                            f"失败: {self.frames_error} | "
                            f"FPS: {actual_fps:.2f}"
                        )
                        
        except websockets.exceptions.WebSocketException as e:
            logger.error(f"❌ WebSocket错误: {e}")
        except Exception as e:
            logger.error(f"❌ 上传循环错误: {e}")
        finally:
            self.websocket = None
            logger.info("🔌 WebSocket连接已关闭")
    
    def _run_async_loop(self):
        """在独立线程中运行异步事件循环"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            self.loop.run_until_complete(self._upload_loop())
        except Exception as e:
            logger.error(f"❌ 异步循环错误: {e}")
        finally:
            self.loop.close()
            self.loop = None
    
    def start(self) -> bool:
        """
        启动摄像头采集和视频上传
        
        Returns:
            bool: 启动是否成功
        """
        if self.is_running:
            logger.warning("⚠️  客户端已经在运行中")
            return False
        
        logger.info("=" * 60)
        logger.info("🚀 启动摄像头客户端")
        logger.info("=" * 60)
        logger.info(f"Client ID: {self.client_id}")
        logger.info(f"服务器: {self.server_url}")
        logger.info(f"摄像头ID: {self.camera_id}")
        logger.info(f"目标FPS: {self.fps}")
        logger.info(f"分辨率: {self.frame_width}x{self.frame_height}")
        logger.info(f"JPEG质量: {self.jpeg_quality}")
        logger.info("=" * 60)
        
        # 初始化摄像头
        if not self._init_camera():
            return False
        
        # 重置统计
        self.frames_sent = 0
        self.frames_success = 0
        self.frames_error = 0
        self.start_time = time.time()
        
        # 启动上传线程
        self.is_running = True
        self.thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.thread.start()
        
        logger.info("✅ 客户端启动成功，开始采集和上传...")
        return True
    
    def stop(self):
        """停止摄像头采集和视频上传"""
        if not self.is_running:
            logger.warning("⚠️  客户端未在运行")
            return
        
        logger.info("🛑 正在停止客户端...")
        
        # 设置停止标志
        self.is_running = False
        
        # 等待上传线程结束
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        
        # 释放摄像头
        if self.camera:
            self.camera.release()
            self.camera = None
            logger.info("✅ 摄像头已释放")
        
        # 输出最终统计
        elapsed_total = time.time() - self.start_time
        avg_fps = self.frames_sent / elapsed_total if elapsed_total > 0 else 0
        success_rate = (self.frames_success / self.frames_sent * 100) if self.frames_sent > 0 else 0
        
        logger.info("=" * 60)
        logger.info("📊 客户端统计")
        logger.info("=" * 60)
        logger.info(f"运行时长: {elapsed_total:.2f} 秒")
        logger.info(f"发送帧数: {self.frames_sent}")
        logger.info(f"成功帧数: {self.frames_success}")
        logger.info(f"失败帧数: {self.frames_error}")
        logger.info(f"成功率: {success_rate:.2f}%")
        logger.info(f"平均FPS: {avg_fps:.2f}")
        logger.info("=" * 60)
        logger.info("✅ 客户端已停止")
    
    def is_active(self) -> bool:
        """
        检查客户端是否正在运行
        
        Returns:
            bool: 是否正在运行
        """
        return self.is_running
    
    def get_stats(self) -> dict:
        """
        获取当前统计信息
        
        Returns:
            dict: 统计信息字典
        """
        elapsed = time.time() - self.start_time if self.is_running else 0
        avg_fps = self.frames_sent / elapsed if elapsed > 0 else 0
        success_rate = (self.frames_success / self.frames_sent * 100) if self.frames_sent > 0 else 0
        
        return {
            "is_running": self.is_running,
            "elapsed_time": elapsed,
            "frames_sent": self.frames_sent,
            "frames_success": self.frames_success,
            "frames_error": self.frames_error,
            "success_rate": success_rate,
            "average_fps": avg_fps
        }


def main():
    """主函数：演示客户端使用"""
    import argparse
    
    parser = argparse.ArgumentParser(description='摄像头采集客户端')
    parser.add_argument('--client-id', '-c', type=str, required=True,
                       help='客户端ID（必需）')
    parser.add_argument('--server-url', '-s', type=str,
                       default='ws://localhost:8000/inspection/upload_stream',
                       help='WebSocket服务器地址')
    parser.add_argument('--camera-id', type=int, default=0,
                       help='摄像头ID (默认: 0)')
    parser.add_argument('--fps', '-f', type=int, default=30,
                       help='采集帧率 (默认: 30)')
    parser.add_argument('--width', '-w', type=int, default=640,
                       help='视频宽度 (默认: 640)')
    parser.add_argument('--height', '-h', type=int, default=480,
                       help='视频高度 (默认: 480)')
    parser.add_argument('--jpeg-quality', '-q', type=int, default=70,
                       help='JPEG质量 (1-100, 默认: 70)')
    parser.add_argument('--duration', '-d', type=int, default=0,
                       help='运行时长（秒），0表示无限运行 (默认: 0)')
    
    args = parser.parse_args()
    
    # 创建客户端
    client = CameraClient(
        client_id=args.client_id,
        server_url=args.server_url,
        camera_id=args.camera_id,
        fps=args.fps,
        jpeg_quality=args.jpeg_quality,
        frame_width=args.width,
        frame_height=args.height
    )
    
    # 启动客户端
    if not client.start():
        logger.error("❌ 客户端启动失败")
        return
    
    try:
        if args.duration > 0:
            # 运行指定时长
            logger.info(f"⏱️  将运行 {args.duration} 秒...")
            time.sleep(args.duration)
        else:
            # 无限运行，按Ctrl+C停止
            logger.info("⏱️  按 Ctrl+C 停止客户端...")
            while True:
                time.sleep(1)
                
    except KeyboardInterrupt:
        logger.info("\n⚠️  收到中断信号")
    finally:
        # 停止客户端
        client.stop()


if __name__ == "__main__":
    main()
