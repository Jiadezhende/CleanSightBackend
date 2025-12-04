"""
独立的推理结果展示客户端

通过 WebSocket 连接后端，实时接收并展示 AI 推理结果
可以作为独立进程运行，用于可视化测试
"""
import asyncio
import websockets
import json
import argparse
import base64
from datetime import datetime
from typing import Optional


class InferenceViewer:
    """推理结果查看器"""
    
    def __init__(self, client_id: str, ws_url: str = "ws://localhost:8000/ai/video"):
        self.client_id = client_id
        self.ws_url = f"{ws_url}?client_id={client_id}"
        self.frame_count = 0
        self.start_time = None
        self.last_print_time = 0
    
    async def connect_and_display(self, duration: Optional[int] = None):
        """连接 WebSocket 并展示推理结果"""
        print(f"📺 连接到 WebSocket: {self.ws_url}")
        print(f"客户端 ID: {self.client_id}")
        print("-" * 60)
        
        try:
            async with websockets.connect(self.ws_url) as websocket:
                print("✅ WebSocket 已连接，开始接收推理结果...\n")
                self.start_time = datetime.now()
                
                while True:
                    # 检查是否超时
                    if duration:
                        elapsed = (datetime.now() - self.start_time).total_seconds()
                        if elapsed >= duration:
                            print(f"\n⏱️ 达到测试时长 {duration} 秒，停止接收")
                            break
                    
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                        self._process_message(message)
                    except asyncio.TimeoutError:
                        # 超时继续等待
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        print("\n⚠️ WebSocket 连接已关闭")
                        break
        
        except Exception as e:
            print(f"\n❌ WebSocket 连接失败: {e}")
        
        finally:
            self._print_summary()
    
    def _process_message(self, message: str):
        """处理接收到的消息"""
        try:
            data = json.loads(message)
            self.frame_count += 1
            
            # 每秒打印一次统计
            current_time = datetime.now().timestamp()
            if current_time - self.last_print_time >= 1.0:
                elapsed = (datetime.now() - self.start_time).total_seconds()
                fps = self.frame_count / max(elapsed, 1)
                
                # 提取推理结果
                inference = data.get('inference_result', {})
                detection = inference.get('detection', {})
                motion = inference.get('motion', {})
                
                # 构建状态行
                status_parts = [
                    f"⏱️  {int(elapsed)}s",
                    f"帧数: {self.frame_count}",
                    f"FPS: {fps:.1f}",
                ]
                
                # 添加检测结果
                if detection.get('success'):
                    keypoints = detection.get('keypoints', {})
                    status_parts.append(f"关键点: {len(keypoints)}")
                
                # 添加动作分析
                if motion.get('success'):
                    actions = motion.get('actions', {})
                    if actions.get('bending_detected'):
                        status_parts.append("🔴 弯曲")
                    if actions.get('bubble_detected'):
                        status_parts.append("💧 气泡")
                    
                    submersion = actions.get('submersion_status', 'unknown')
                    if submersion != 'unknown':
                        status_parts.append(f"浸没: {submersion}")
                
                print(" | ".join(status_parts))
                self.last_print_time = current_time
        
        except json.JSONDecodeError:
            print("⚠️ 无效的 JSON 消息")
        except Exception as e:
            print(f"⚠️ 处理消息失败: {e}")
    
    def _print_summary(self):
        """打印统计摘要"""
        if self.start_time:
            elapsed = (datetime.now() - self.start_time).total_seconds()
            avg_fps = self.frame_count / max(elapsed, 1)
            
            print("\n" + "=" * 60)
            print("📊 推理结果接收统计")
            print("=" * 60)
            print(f"运行时长: {elapsed:.1f} 秒")
            print(f"接收帧数: {self.frame_count} 帧")
            print(f"平均帧率: {avg_fps:.1f} FPS")
            print("=" * 60)


async def main():
    parser = argparse.ArgumentParser(description="推理结果展示客户端")
    parser.add_argument("--client_id", type=str, default="test_client",
                       help="客户端 ID")
    parser.add_argument("--ws_url", type=str, default="ws://localhost:8000/ai/video",
                       help="WebSocket URL")
    parser.add_argument("--duration", type=int, default=None,
                       help="运行时长（秒），不指定则持续运行")
    
    args = parser.parse_args()
    
    viewer = InferenceViewer(args.client_id, args.ws_url)
    
    try:
        await viewer.connect_and_display(args.duration)
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断")


if __name__ == "__main__":
    asyncio.run(main())
