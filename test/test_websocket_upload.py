"""
WebSocket 上传视频流测试脚本

测试 /inspection/upload_stream 接口
读取本地视频文件，逐帧通过 WebSocket 发送到服务器
"""

import asyncio
import base64
import cv2
import time
from pathlib import Path
import websockets
import argparse
import sys
from collections import deque
from typing import Optional


class VideoStreamTester:
    """视频流 WebSocket 测试器"""
    
    def __init__(self, 
                 video_path: str,
                 ws_url: str = "ws://localhost:8000/inspection/upload_stream",
                 client_id: str = "test_client_001",
                 fps: int = 30,
                 show_preview: bool = False,
                 jpeg_quality: int = 70,
                 async_mode: bool = True):
        """
        Args:
            video_path: 视频文件路径
            ws_url: WebSocket 服务器地址
            client_id: 客户端ID
            fps: 发送帧率（每秒发送多少帧）
            show_preview: 是否显示预览窗口
            jpeg_quality: JPEG编码质量 (1-100, 推荐60-75)
            async_mode: 是否使用异步模式（不等待响应，大幅提升性能）
        """
        self.video_path = Path(video_path)
        self.ws_url = ws_url
        self.client_id = client_id
        self.fps = fps
        self.show_preview = show_preview
        self.jpeg_quality = jpeg_quality
        self.async_mode = async_mode
        self.frame_interval = 1.0 / fps if fps > 0 else 0
        
        # 统计信息
        self.total_frames = 0
        self.sent_frames = 0
        self.success_frames = 0
        self.error_frames = 0
        self.start_time = None
        
        # 异步模式的响应队列
        self.response_queue: Optional[asyncio.Queue] = None
        self.response_task: Optional[asyncio.Task] = None
        
    def validate_video(self) -> bool:
        """验证视频文件是否有效"""
        if not self.video_path.exists():
            print(f"❌ 视频文件不存在: {self.video_path}")
            return False
        
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            print(f"❌ 无法打开视频文件: {self.video_path}")
            return False
        
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = self.total_frames / video_fps if video_fps > 0 else 0
        
        print(f"✅ 视频文件信息:")
        print(f"   路径: {self.video_path}")
        print(f"   分辨率: {width}x{height}")
        print(f"   原始FPS: {video_fps:.2f}")
        print(f"   总帧数: {self.total_frames}")
        print(f"   时长: {duration:.2f}秒")
        print(f"   发送FPS: {self.fps}")
        
        cap.release()
        return True
    
    def encode_frame(self, frame) -> str:
        """将视频帧编码为 Base64 字符串"""
        # 编码为 JPEG 格式（优化：降低质量以提升速度）
        _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        # 转换为 Base64
        frame_b64 = base64.b64encode(buffer).decode('utf-8')
        return frame_b64
    
    async def response_handler(self, websocket: websockets.WebSocketClientProtocol):
        """异步响应处理器（后台任务）"""
        try:
            while True:
                response = await websocket.recv()
                if response == "success":
                    self.success_frames += 1
                else:
                    self.error_frames += 1
        except asyncio.CancelledError:
            # 任务被取消，正常退出
            pass
        except Exception as e:
            print(f"⚠️  响应处理器错误: {e}")
    
    async def send_video_stream(self):
        """通过 WebSocket 发送视频流"""
        # 构建完整的 WebSocket URL（包含 client_id）
        full_url = f"{self.ws_url}?client_id={self.client_id}"
        print(f"\n🔌 正在连接到 WebSocket: {full_url}")
        
        mode_text = "异步模式（高性能）" if self.async_mode else "同步模式（等待响应）"
        print(f"⚙️  传输模式: {mode_text}")
        print(f"⚙️  JPEG质量: {self.jpeg_quality}%")
        
        try:
            async with websockets.connect(full_url) as websocket:
                print(f"✅ WebSocket 连接成功!")
                print(f"📤 开始发送视频帧...\n")
                
                # 启动异步响应处理器
                if self.async_mode:
                    self.response_task = asyncio.create_task(self.response_handler(websocket))
                
                # 打开视频文件
                cap = cv2.VideoCapture(str(self.video_path))
                self.start_time = time.time()
                
                frame_count = 0
                next_frame_time = self.start_time
                
                while cap.isOpened():
                    ret, frame = cap.read()
                    if not ret:
                        print("\n📹 视频播放完毕")
                        break
                    
                    frame_count += 1
                    
                    # 精确时间控制：基于目标时间而非固定间隔
                    current_time = time.time()
                    if self.frame_interval > 0:
                        sleep_time = next_frame_time - current_time
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                        next_frame_time += self.frame_interval
                    
                    # 编码帧为 Base64
                    encode_start = time.time()
                    frame_b64 = self.encode_frame(frame)
                    encode_time = time.time() - encode_start
                    
                    # 发送帧
                    try:
                        send_start = time.time()
                        await websocket.send(frame_b64)
                        self.sent_frames += 1
                        send_time = time.time() - send_start
                        
                        if self.async_mode:
                            # 异步模式：立即继续，不等待响应
                            pass
                        else:
                            # 同步模式：等待响应
                            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                            
                            if response == "success":
                                self.success_frames += 1
                            else:
                                self.error_frames += 1
                                print(f"⚠️  帧 {frame_count} 服务器返回错误: {response}")
                        
                        # 每 30 帧打印一次进度
                        if frame_count % 30 == 0:
                            elapsed = time.time() - self.start_time
                            fps_actual = self.sent_frames / elapsed if elapsed > 0 else 0
                            print(f"📊 进度: {frame_count}/{self.total_frames} 帧 | "
                                  f"已发送: {self.sent_frames} | "
                                  f"成功: {self.success_frames} | "
                                  f"失败: {self.error_frames} | "
                                  f"实际FPS: {fps_actual:.2f} | "
                                  f"编码: {encode_time*1000:.1f}ms | "
                                  f"发送: {send_time*1000:.1f}ms")
                    
                    except asyncio.TimeoutError:
                        print(f"⚠️  帧 {frame_count} 服务器响应超时")
                        self.error_frames += 1
                    except Exception as e:
                        print(f"❌ 帧 {frame_count} 发送失败: {e}")
                        self.error_frames += 1
                    
                    # 预览（可选）
                    if self.show_preview:
                        cv2.imshow('Test Video Stream', frame)
                        if cv2.waitKey(1) & 0xFF == ord('q'):
                            print("\n⏹️  用户中断")
                            break
                
                # 等待剩余响应处理完成
                if self.async_mode and self.response_task:
                    await asyncio.sleep(0.5)  # 给一点时间处理剩余响应
                    self.response_task.cancel()
                    try:
                        await self.response_task
                    except asyncio.CancelledError:
                        pass
                
                cap.release()
                if self.show_preview:
                    cv2.destroyAllWindows()
                
                # 打印最终统计
                self.print_statistics()
                
        except websockets.exceptions.WebSocketException as e:
            print(f"❌ WebSocket 连接错误: {e}")
            return False
        except Exception as e:
            print(f"❌ 发生错误: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        return True
    
    def print_statistics(self):
        """打印统计信息"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        avg_fps = self.sent_frames / elapsed if elapsed > 0 else 0
        success_rate = (self.success_frames / self.sent_frames * 100) if self.sent_frames > 0 else 0
        
        print("\n" + "="*60)
        print("📊 测试统计")
        print("="*60)
        print(f"总耗时:      {elapsed:.2f} 秒")
        print(f"发送帧数:    {self.sent_frames}")
        print(f"成功帧数:    {self.success_frames}")
        print(f"失败帧数:    {self.error_frames}")
        print(f"成功率:      {success_rate:.2f}%")
        print(f"平均FPS:     {avg_fps:.2f}")
        print("="*60)
    
    async def run(self):
        """运行测试"""
        print("\n" + "="*60)
        print("🧪 WebSocket 视频流上传测试")
        print("="*60)
        
        # 验证视频
        if not self.validate_video():
            return False
        
        # 发送视频流
        print(f"\n⚙️  测试配置:")
        print(f"   WebSocket URL: {self.ws_url}")
        print(f"   Client ID: {self.client_id}")
        print(f"   目标FPS: {self.fps}")
        print(f"   预览模式: {'开启' if self.show_preview else '关闭'}")
        
        result = await self.send_video_stream()
        
        if result:
            print("\n✅ 测试完成!")
        else:
            print("\n❌ 测试失败!")
        
        return result


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='WebSocket 视频流上传测试脚本')
    
    # 获取当前脚本所在目录
    script_dir = Path(__file__).parent
    default_video = script_dir / "test_video.mp4"
    
    parser.add_argument('--video', '-v',
                       type=str,
                       default=str(default_video),
                       help=f'视频文件路径 (默认: {default_video})')
    
    parser.add_argument('--url', '-u',
                       type=str,
                       default='ws://localhost:8000/inspection/upload_stream',
                       help='WebSocket 服务器地址 (默认: ws://localhost:8000/inspection/upload_stream)')
    
    parser.add_argument('--client-id', '-c',
                       type=str,
                       default='test_client_001',
                       help='客户端ID (默认: test_client_001)')
    
    parser.add_argument('--fps', '-f',
                       type=int,
                       default=30,
                       help='发送帧率 (默认: 30)')
    
    parser.add_argument('--preview', '-p',
                       action='store_true',
                       help='显示视频预览窗口')
    
    parser.add_argument('--jpeg-quality', '-q',
                       type=int,
                       default=70,
                       help='JPEG编码质量 (1-100, 推荐60-75) (默认: 70)')
    
    parser.add_argument('--sync-mode',
                       action='store_true',
                       help='使用同步模式（等待每帧响应，较慢但更安全）')
    
    args = parser.parse_args()
    
    # 创建测试器并运行
    tester = VideoStreamTester(
        video_path=args.video,
        ws_url=args.url,
        client_id=args.client_id,
        fps=args.fps,
        show_preview=args.preview,
        jpeg_quality=args.jpeg_quality,
        async_mode=not args.sync_mode  # 默认异步，除非指定同步
    )
    
    success = await tester.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
