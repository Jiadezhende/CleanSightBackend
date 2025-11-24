"""
WebSocket 接收视频流测试脚本

测试 /ai/video 接口
接收服务器推送的处理后的视频帧并保存/显示
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


class VideoReceiveTester:
    """视频流接收 WebSocket 测试器"""
    
    def __init__(self, 
                 ws_url: str = "ws://localhost:8000/ai/video",
                 client_id: str = "test_client_001",
                 save_frames: bool = False,
                 output_dir: str = "./test_output",
                 show_preview: bool = True,
                 duration: int = 30):
        """
        Args:
            ws_url: WebSocket 服务器地址
            client_id: 客户端ID（需要与上传端使用相同的ID）
            save_frames: 是否保存接收到的帧
            output_dir: 输出目录
            show_preview: 是否显示预览窗口
            duration: 测试持续时间（秒）
        """
        self.ws_url = ws_url
        self.client_id = client_id
        self.save_frames = save_frames
        self.output_dir = Path(output_dir)
        self.show_preview = show_preview
        self.duration = duration
        
        # 统计信息
        self.received_frames = 0
        self.error_frames = 0
        self.start_time = None
        self.last_frame_time = None
        
        # 如果需要保存帧，创建输出目录
        if self.save_frames:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_dir = self.output_dir / f"session_{self.client_id}_{timestamp}"
            self.session_dir.mkdir(parents=True, exist_ok=True)
    
    def decode_frame(self, data_url: str) -> np.ndarray:
        """解码 Base64 数据URL为图像"""
        # 移除 data:image/jpeg;base64, 前缀
        if data_url.startswith("data:image/jpeg;base64,"):
            base64_str = data_url[len("data:image/jpeg;base64,"):]
        else:
            base64_str = data_url
        
        # Base64 解码
        img_data = base64.b64decode(base64_str)
        
        # 转换为 numpy 数组
        np_arr = np.frombuffer(img_data, np.uint8)
        
        # 解码为图像
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        
        return img
    
    async def receive_video_stream(self):
        """通过 WebSocket 接收视频流"""
        # 构建完整的 WebSocket URL（包含 client_id）
        full_url = f"{self.ws_url}?client_id={self.client_id}"
        print(f"\n🔌 正在连接到 WebSocket: {full_url}")
        
        try:
            async with websockets.connect(full_url) as websocket:
                print(f"✅ WebSocket 连接成功!")
                print(f"📥 开始接收处理后的视频帧...\n")
                
                self.start_time = time.time()
                self.last_frame_time = self.start_time
                
                while True:
                    # 检查是否超时
                    elapsed = time.time() - self.start_time
                    if self.duration > 0 and elapsed >= self.duration:
                        print(f"\n⏱️  已达到设定时长 {self.duration} 秒")
                        break
                    
                    try:
                        # 接收帧（带超时）
                        data_url = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                        
                        current_time = time.time()
                        frame_interval = current_time - self.last_frame_time
                        self.last_frame_time = current_time
                        
                        # 解码帧
                        frame = self.decode_frame(data_url)
                        
                        if frame is None:
                            print(f"⚠️  帧 {self.received_frames + 1} 解码失败")
                            self.error_frames += 1
                            continue
                        
                        self.received_frames += 1
                        
                        # 计算实时 FPS
                        fps = 1.0 / frame_interval if frame_interval > 0 else 0
                        
                        # 每 30 帧打印一次进度
                        if self.received_frames % 30 == 0:
                            avg_fps = self.received_frames / elapsed if elapsed > 0 else 0
                            print(f"📊 接收: {self.received_frames} 帧 | "
                                  f"失败: {self.error_frames} | "
                                  f"即时FPS: {fps:.2f} | "
                                  f"平均FPS: {avg_fps:.2f} | "
                                  f"耗时: {elapsed:.1f}s")
                        
                        # 保存帧（如果启用）
                        if self.save_frames:
                            frame_path = self.session_dir / f"frame_{self.received_frames:06d}.jpg"
                            cv2.imwrite(str(frame_path), frame)
                        
                        # 显示预览（如果启用）
                        if self.show_preview:
                            # 在帧上添加信息
                            info_frame = frame.copy()
                            cv2.putText(info_frame, f"Frame: {self.received_frames}", 
                                       (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                            cv2.putText(info_frame, f"FPS: {fps:.2f}", 
                                       (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                            cv2.putText(info_frame, f"Time: {elapsed:.1f}s", 
                                       (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                            
                            cv2.imshow('AI Processed Video Stream', info_frame)
                            if cv2.waitKey(1) & 0xFF == ord('q'):
                                print("\n⏹️  用户中断")
                                break
                    
                    except asyncio.TimeoutError:
                        elapsed = time.time() - self.start_time
                        if elapsed < 5.0:
                            # 刚开始连接，可能还没有数据
                            print("⏳ 等待服务器推送数据...")
                            continue
                        else:
                            print("⚠️  接收超时（5秒内未收到数据）")
                            continue
                    except Exception as e:
                        print(f"❌ 处理帧时出错: {e}")
                        self.error_frames += 1
                
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
        avg_fps = self.received_frames / elapsed if elapsed > 0 else 0
        success_rate = ((self.received_frames - self.error_frames) / self.received_frames * 100) if self.received_frames > 0 else 0
        
        print("\n" + "="*60)
        print("📊 接收统计")
        print("="*60)
        print(f"总耗时:      {elapsed:.2f} 秒")
        print(f"接收帧数:    {self.received_frames}")
        print(f"错误帧数:    {self.error_frames}")
        print(f"成功率:      {success_rate:.2f}%")
        print(f"平均FPS:     {avg_fps:.2f}")
        if self.save_frames:
            print(f"保存位置:    {self.session_dir}")
        print("="*60)
    
    async def run(self):
        """运行测试"""
        print("\n" + "="*60)
        print("🧪 WebSocket 视频流接收测试")
        print("="*60)
        
        print(f"\n⚙️  测试配置:")
        print(f"   WebSocket URL: {self.ws_url}")
        print(f"   Client ID: {self.client_id}")
        print(f"   测试时长: {self.duration}秒 (0=无限制)")
        print(f"   预览模式: {'开启' if self.show_preview else '关闭'}")
        print(f"   保存帧: {'是' if self.save_frames else '否'}")
        
        result = await self.receive_video_stream()
        
        if result:
            print("\n✅ 测试完成!")
        else:
            print("\n❌ 测试失败!")
        
        return result


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='WebSocket 视频流接收测试脚本')
    
    parser.add_argument('--url', '-u',
                       type=str,
                       default='ws://localhost:8000/ai/video',
                       help='WebSocket 服务器地址 (默认: ws://localhost:8000/ai/video)')
    
    parser.add_argument('--client-id', '-c',
                       type=str,
                       default='test_client_001',
                       help='客户端ID (需要与上传端使用相同的ID) (默认: test_client_001)')
    
    parser.add_argument('--duration', '-d',
                       type=int,
                       default=30,
                       help='测试持续时间（秒）(0=无限制) (默认: 30)')
    
    parser.add_argument('--save', '-s',
                       action='store_true',
                       help='保存接收到的帧')
    
    parser.add_argument('--output', '-o',
                       type=str,
                       default='./test_output',
                       help='输出目录 (默认: ./test_output)')
    
    parser.add_argument('--no-preview',
                       action='store_true',
                       help='不显示预览窗口')
    
    args = parser.parse_args()
    
    # 创建测试器并运行
    tester = VideoReceiveTester(
        ws_url=args.url,
        client_id=args.client_id,
        save_frames=args.save,
        output_dir=args.output,
        show_preview=not args.no_preview,
        duration=args.duration
    )
    
    success = await tester.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
