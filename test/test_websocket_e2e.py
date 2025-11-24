"""
完整端到端 WebSocket 测试脚本

同时测试两个接口：
1. /inspection/upload_stream - 上传视频帧
2. /ai/video - 接收处理后的视频帧

使用两个并发任务来模拟真实场景
"""

import asyncio
import base64
import cv2
import time
from pathlib import Path
import websockets
import argparse
import sys
import numpy as np
from datetime import datetime
from typing import Optional


class EndToEndTester:
    """端到端 WebSocket 测试器"""
    
    def __init__(self,
                 video_path: str,
                 upload_url: str = "ws://localhost:8000/inspection/upload_stream",
                 receive_url: str = "ws://localhost:8000/ai/video",
                 client_id: str = "test_client_e2e",
                 fps: int = 30,
                 save_output: bool = False,
                 output_dir: str = "./test_output",
                 show_preview: bool = True,
                 jpeg_quality: int = 70,
                 async_mode: bool = True):
        """
        Args:
            video_path: 输入视频文件路径
            upload_url: 上传 WebSocket 地址
            receive_url: 接收 WebSocket 地址
            client_id: 客户端ID（上传和接收使用同一个ID）
            fps: 发送帧率
            save_output: 是否保存处理后的帧
            output_dir: 输出目录
            show_preview: 是否显示预览窗口
            jpeg_quality: JPEG编码质量 (1-100)
            async_mode: 是否使用异步模式（不等待响应）
        """
        self.video_path = Path(video_path)
        self.upload_url = upload_url
        self.receive_url = receive_url
        self.client_id = client_id
        self.fps = fps
        self.save_output = save_output
        self.output_dir = Path(output_dir)
        self.show_preview = show_preview
        self.jpeg_quality = jpeg_quality
        self.async_mode = async_mode
        self.frame_interval = 1.0 / fps if fps > 0 else 0
        
        # 统计信息
        self.uploaded_frames = 0
        self.upload_success = 0
        self.upload_errors = 0
        self.received_frames = 0
        self.receive_errors = 0
        self.start_time = None
        self.upload_done = False
        
        # 异步模式的响应任务
        self.response_task: Optional[asyncio.Task] = None
        
        # 输出目录
        if self.save_output:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_dir = self.output_dir / f"e2e_{self.client_id}_{timestamp}"
            self.session_dir.mkdir(parents=True, exist_ok=True)
    
    def validate_video(self) -> bool:
        """验证视频文件"""
        if not self.video_path.exists():
            print(f"❌ 视频文件不存在: {self.video_path}")
            return False
        
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            print(f"❌ 无法打开视频文件: {self.video_path}")
            return False
        
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = total_frames / video_fps if video_fps > 0 else 0
        
        print(f"✅ 视频文件信息:")
        print(f"   路径: {self.video_path}")
        print(f"   分辨率: {width}x{height}")
        print(f"   原始FPS: {video_fps:.2f}")
        print(f"   总帧数: {total_frames}")
        print(f"   时长: {duration:.2f}秒")
        
        cap.release()
        return True
    
    def encode_frame(self, frame) -> str:
        """将视频帧编码为 Base64"""
        _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        frame_b64 = base64.b64encode(buffer).decode('utf-8')
        return frame_b64
    
    async def response_handler(self, websocket):
        """异步响应处理器"""
        try:
            while True:
                response = await websocket.recv()
                if response == "success":
                    self.upload_success += 1
                else:
                    self.upload_errors += 1
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"⚠️  [上传] 响应处理器错误: {e}")
    
    def decode_frame(self, data_url: str) -> Optional[np.ndarray]:
        """解码 Base64 数据URL为图像"""
        try:
            if data_url.startswith("data:image/jpeg;base64,"):
                base64_str = data_url[len("data:image/jpeg;base64,"):]
            else:
                base64_str = data_url
            
            img_data = base64.b64decode(base64_str)
            np_arr = np.frombuffer(img_data, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            return img
        except Exception as e:
            print(f"解码失败: {e}")
            return None
    
    async def upload_task(self):
        """上传任务：发送视频帧"""
        full_url = f"{self.upload_url}?client_id={self.client_id}"
        print(f"📤 [上传] 正在连接到: {full_url}")
        
        mode_text = "异步模式" if self.async_mode else "同步模式"
        print(f"📤 [上传] 传输模式: {mode_text}, JPEG质量: {self.jpeg_quality}%")
        
        try:
            async with websockets.connect(full_url) as websocket:
                print(f"✅ [上传] WebSocket 连接成功")
                
                # 启动异步响应处理器
                if self.async_mode:
                    self.response_task = asyncio.create_task(self.response_handler(websocket))
                
                cap = cv2.VideoCapture(str(self.video_path))
                frame_count = 0
                next_frame_time = time.time()
                
                while cap.isOpened():
                    ret, frame = cap.read()
                    if not ret:
                        print("\n📹 [上传] 视频播放完毕")
                        break
                    
                    frame_count += 1
                    
                    # 精确时间控制
                    current_time = time.time()
                    if self.frame_interval > 0:
                        sleep_time = next_frame_time - current_time
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                        next_frame_time += self.frame_interval
                    
                    # 编码并发送
                    frame_b64 = self.encode_frame(frame)
                    
                    try:
                        await websocket.send(frame_b64)
                        self.uploaded_frames += 1
                        
                        if self.async_mode:
                            # 异步模式：立即继续
                            pass
                        else:
                            # 同步模式：等待响应
                            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                            
                            if response == "success":
                                self.upload_success += 1
                            else:
                                self.upload_errors += 1
                                print(f"⚠️  [上传] 帧 {frame_count} 服务器返回错误: {response}")
                        
                        # 每 30 帧打印一次进度
                        if frame_count % 30 == 0:
                            elapsed = time.time() - self.start_time
                            fps_actual = self.uploaded_frames / elapsed if elapsed > 0 else 0
                            print(f"📤 [上传] 进度: {frame_count} 帧 | "
                                  f"已发送: {self.uploaded_frames} | "
                                  f"成功: {self.upload_success} | "
                                  f"FPS: {fps_actual:.2f}")
                    
                    except asyncio.TimeoutError:
                        print(f"⚠️  [上传] 帧 {frame_count} 响应超时")
                        self.upload_errors += 1
                    except Exception as e:
                        print(f"❌ [上传] 帧 {frame_count} 发送失败: {e}")
                        self.upload_errors += 1
                
                # 等待剩余响应处理完成
                if self.async_mode and self.response_task:
                    await asyncio.sleep(0.5)
                    self.response_task.cancel()
                    try:
                        await self.response_task
                    except asyncio.CancelledError:
                        pass
                
                cap.release()
                self.upload_done = True
                print(f"✅ [上传] 完成，共发送 {self.uploaded_frames} 帧")
                
        except Exception as e:
            print(f"❌ [上传] 连接错误: {e}")
            import traceback
            traceback.print_exc()
    
    async def receive_task(self):
        """接收任务：接收处理后的视频帧"""
        # 等待一小段时间，确保上传任务先启动
        await asyncio.sleep(2)
        
        full_url = f"{self.receive_url}?client_id={self.client_id}"
        print(f"📥 [接收] 正在连接到: {full_url}")
        
        try:
            async with websockets.connect(full_url) as websocket:
                print(f"✅ [接收] WebSocket 连接成功")
                
                last_update = time.time()
                
                while not self.upload_done or self.received_frames < self.uploaded_frames:
                    try:
                        # 接收帧
                        data_url = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                        
                        # 解码帧
                        frame = self.decode_frame(data_url)
                        
                        if frame is not None:
                            self.received_frames += 1
                            
                            # 保存帧
                            if self.save_output:
                                frame_path = self.session_dir / f"processed_{self.received_frames:06d}.jpg"
                                cv2.imwrite(str(frame_path), frame)
                            
                            # 显示预览
                            if self.show_preview:
                                # 在帧上添加信息
                                info_frame = frame.copy()
                                elapsed = time.time() - self.start_time
                                fps = self.received_frames / elapsed if elapsed > 0 else 0
                                
                                cv2.putText(info_frame, f"Received: {self.received_frames}/{self.uploaded_frames}", 
                                           (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                                cv2.putText(info_frame, f"FPS: {fps:.2f}", 
                                           (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                                cv2.putText(info_frame, f"Time: {elapsed:.1f}s", 
                                           (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                                
                                cv2.imshow('E2E Test - Processed Video', info_frame)
                                if cv2.waitKey(1) & 0xFF == ord('q'):
                                    print("\n⏹️  [接收] 用户中断")
                                    break
                            
                            # 每 30 帧打印一次进度
                            if self.received_frames % 30 == 0:
                                elapsed = time.time() - self.start_time
                                fps = self.received_frames / elapsed if elapsed > 0 else 0
                                print(f"📥 [接收] 进度: {self.received_frames} 帧 | FPS: {fps:.2f}")
                        else:
                            self.receive_errors += 1
                    
                    except asyncio.TimeoutError:
                        current_time = time.time()
                        if current_time - last_update > 5.0:
                            print("⏳ [接收] 等待服务器推送数据...")
                            last_update = current_time
                        continue
                    except Exception as e:
                        print(f"❌ [接收] 处理帧时出错: {e}")
                        self.receive_errors += 1
                
                if self.show_preview:
                    cv2.destroyAllWindows()
                
                print(f"✅ [接收] 完成，共接收 {self.received_frames} 帧")
                
        except Exception as e:
            print(f"❌ [接收] 连接错误: {e}")
            import traceback
            traceback.print_exc()
    
    def print_statistics(self):
        """打印统计信息"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        
        upload_fps = self.uploaded_frames / elapsed if elapsed > 0 else 0
        receive_fps = self.received_frames / elapsed if elapsed > 0 else 0
        upload_success_rate = (self.upload_success / self.uploaded_frames * 100) if self.uploaded_frames > 0 else 0
        processing_rate = (self.received_frames / self.uploaded_frames * 100) if self.uploaded_frames > 0 else 0
        
        print("\n" + "="*60)
        print("📊 端到端测试统计")
        print("="*60)
        print(f"总耗时:          {elapsed:.2f} 秒")
        print("")
        print("【上传】")
        print(f"  发送帧数:      {self.uploaded_frames}")
        print(f"  成功帧数:      {self.upload_success}")
        print(f"  失败帧数:      {self.upload_errors}")
        print(f"  成功率:        {upload_success_rate:.2f}%")
        print(f"  平均FPS:       {upload_fps:.2f}")
        print("")
        print("【接收】")
        print(f"  接收帧数:      {self.received_frames}")
        print(f"  错误帧数:      {self.receive_errors}")
        print(f"  处理率:        {processing_rate:.2f}%")
        print(f"  平均FPS:       {receive_fps:.2f}")
        print("")
        print("【延迟】")
        if self.uploaded_frames > 0 and self.received_frames > 0:
            latency = (self.uploaded_frames - self.received_frames) / upload_fps if upload_fps > 0 else 0
            print(f"  帧差:          {self.uploaded_frames - self.received_frames}")
            print(f"  估计延迟:      {latency:.2f} 秒")
        
        if self.save_output:
            print("")
            print(f"输出保存位置:    {self.session_dir}")
        print("="*60)
    
    async def run(self):
        """运行端到端测试"""
        print("\n" + "="*60)
        print("🧪 WebSocket 端到端测试")
        print("="*60)
        
        # 验证视频
        if not self.validate_video():
            return False
        
        print(f"\n⚙️  测试配置:")
        print(f"   Client ID: {self.client_id}")
        print(f"   上传URL: {self.upload_url}")
        print(f"   接收URL: {self.receive_url}")
        print(f"   目标FPS: {self.fps}")
        print(f"   传输模式: {'异步' if self.async_mode else '同步'}")
        print(f"   JPEG质量: {self.jpeg_quality}%")
        print(f"   预览模式: {'开启' if self.show_preview else '关闭'}")
        print(f"   保存输出: {'是' if self.save_output else '否'}")
        
        print("\n🚀 开始测试...\n")
        
        self.start_time = time.time()
        
        # 并发运行上传和接收任务
        try:
            await asyncio.gather(
                self.upload_task(),
                self.receive_task()
            )
        except Exception as e:
            print(f"❌ 测试过程中出错: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # 打印统计
        self.print_statistics()
        
        print("\n✅ 端到端测试完成!")
        return True


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='WebSocket 端到端测试脚本')
    
    # 获取当前脚本所在目录
    script_dir = Path(__file__).parent
    default_video = script_dir / "test_video.mp4"
    
    parser.add_argument('--video', '-v',
                       type=str,
                       default=str(default_video),
                       help=f'视频文件路径 (默认: {default_video})')
    
    parser.add_argument('--upload-url',
                       type=str,
                       default='ws://localhost:8000/inspection/upload_stream',
                       help='上传 WebSocket 地址')
    
    parser.add_argument('--receive-url',
                       type=str,
                       default='ws://localhost:8000/ai/video',
                       help='接收 WebSocket 地址')
    
    parser.add_argument('--client-id', '-c',
                       type=str,
                       default='test_client_e2e',
                       help='客户端ID (默认: test_client_e2e)')
    
    parser.add_argument('--fps', '-f',
                       type=int,
                       default=30,
                       help='发送帧率 (默认: 30)')
    
    parser.add_argument('--save', '-s',
                       action='store_true',
                       help='保存处理后的帧')
    
    parser.add_argument('--output', '-o',
                       type=str,
                       default='./test_output',
                       help='输出目录 (默认: ./test_output)')
    
    parser.add_argument('--no-preview',
                       action='store_true',
                       help='不显示预览窗口')
    
    parser.add_argument('--jpeg-quality', '-q',
                       type=int,
                       default=70,
                       help='JPEG编码质量 (1-100) (默认: 70)')
    
    parser.add_argument('--sync-mode',
                       action='store_true',
                       help='使用同步模式（等待响应，较慢但更安全）')
    
    args = parser.parse_args()
    
    # 创建测试器并运行
    tester = EndToEndTester(
        video_path=args.video,
        upload_url=args.upload_url,
        receive_url=args.receive_url,
        client_id=args.client_id,
        fps=args.fps,
        save_output=args.save,
        output_dir=args.output,
        show_preview=not args.no_preview,
        jpeg_quality=args.jpeg_quality,
        async_mode=not args.sync_mode
    )
    
    success = await tester.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
