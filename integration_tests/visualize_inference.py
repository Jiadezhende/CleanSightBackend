"""
独立推理结果可视化脚本

用于连接到指定服务器和客户端，实时查看推理结果。

使用示例：
    # 基本使用
    python visualize_inference.py --client_id rtsp.test.1

    # 指定服务器地址
    python visualize_inference.py --server http://localhost:8000 --client_id rtsp.test.1

    # 不显示窗口，仅统计
    python visualize_inference.py --client_id rtsp.test.1 --no-window

    # 指定运行时长
    python visualize_inference.py --client_id rtsp.test.1 --duration 60
"""
import asyncio
import argparse
import base64
import cv2
import numpy as np
import websockets
from datetime import datetime, timedelta


class InferenceVisualizer:
    """推理结果可视化器"""

    def __init__(self, server_url: str, client_id: str, show_window: bool = True):
        self.server_url = server_url.rstrip('/')
        self.client_id = client_id
        self.show_window = show_window
        self.window_name = f"推理结果 - {client_id}"

        # 统计信息
        self.frame_count = 0
        self.start_time = None
        self.last_print_time = 0
        self.last_second_frames = 0

    async def run(self, duration_seconds: int = None):
        """运行可视化"""
        # 构建 WebSocket URL
        ws_url = f"ws://{self.server_url}/ai/video?client_id={self.client_id}"

        print(f"🚀 推理结果可视化")
        print(f"📡 服务器: {self.server_url}")
        print(f"🆔 客户端: {self.client_id}")
        print(f"🔗 连接到: {ws_url}")
        print("-" * 60)

        self.start_time = datetime.now()
        self.last_print_time = datetime.now().timestamp()

        try:
            async with websockets.connect(ws_url) as websocket:
                print("✅ WebSocket 连接成功")
                if self.show_window:
                    print("💡 提示: 按 'q' 键退出窗口")
                print("-" * 60)

                end_time = None
                if duration_seconds:
                    end_time = datetime.now() + timedelta(seconds=duration_seconds)
                    print(f"⏱️  将运行 {duration_seconds} 秒")

                while True:
                    # 检查是否超时
                    if end_time and datetime.now() > end_time:
                        print("\n⏰ 达到指定运行时长")
                        break

                    # 接收消息
                    try:
                        message = await websocket.recv()
                        if not await self._process_message(message):
                            break
                    except websockets.exceptions.ConnectionClosed:
                        print("\n⚠️ WebSocket 连接已关闭")
                        break

        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断")
        except Exception as e:
            print(f"\n❌ 错误: {e}")
        finally:
            self._cleanup()

    async def _process_message(self, message: str) -> bool:
        """处理接收到的消息"""
        self.frame_count += 1
        self.last_second_frames += 1

        # 处理 Base64 图像
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
                        print("\n👋 用户退出")
                        return False

            except Exception as e:
                print(f"⚠️ 图像解码失败: {e}")

        # 每秒打印统计信息
        current_time = datetime.now().timestamp()
        if current_time - self.last_print_time >= 1.0:
            self._print_stats(current_time)

        return True

    def _print_stats(self, current_time: float):
        """打印统计信息"""
        # 计算瞬时 FPS
        interval = current_time - self.last_print_time
        instant_fps = self.last_second_frames / interval

        # 计算平均 FPS
        elapsed = (datetime.now() - self.start_time).total_seconds()
        avg_fps = self.frame_count / max(elapsed, 1)

        # 格式化运行时间
        elapsed_str = str(timedelta(seconds=int(elapsed)))

        print(f"📊 运行时间: {elapsed_str} | 总帧数: {self.frame_count:5d} | "
              f"瞬时FPS: {instant_fps:5.1f} | 平均FPS: {avg_fps:5.1f}")

        # 重置计数器
        self.last_second_frames = 0
        self.last_print_time = current_time

    def _cleanup(self):
        """清理资源"""
        if self.show_window:
            cv2.destroyAllWindows()

        # 打印最终统计
        if self.start_time:
            elapsed = (datetime.now() - self.start_time).total_seconds()
            avg_fps = self.frame_count / max(elapsed, 1)
            print("\n" + "=" * 60)
            print("📈 最终统计:")
            print(f"   总运行时间: {timedelta(seconds=int(elapsed))}")
            print(f"   总帧数: {self.frame_count}")
            print(f"   平均帧率: {avg_fps:.2f} FPS")
            print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="CleanSight 推理结果可视化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 连接到本地服务器查看客户端推理结果
  %(prog)s --client_id rtsp.test.1

  # 指定远程服务器
  %(prog)s --server 192.168.1.100:8000 --client_id camera_01

  # 仅显示统计，不显示图像窗口
  %(prog)s --client_id rtsp.test.1 --no-window

  # 运行 60 秒后自动退出
  %(prog)s --client_id rtsp.test.1 --duration 60
        """
    )

    parser.add_argument(
        "--server",
        type=str,
        default="localhost:8000",
        help="后端服务器地址 (默认: localhost:8000)"
    )

    parser.add_argument(
        "--client_id",
        type=str,
        required=True,
        help="客户端 ID (必需)"
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="运行时长(秒)，不指定则持续运行"
    )

    parser.add_argument(
        "--no-window",
        action="store_true",
        help="不显示可视化窗口，仅在控制台显示统计信息"
    )

    args = parser.parse_args()

    # 创建可视化器
    visualizer = InferenceVisualizer(
        server_url=args.server,
        client_id=args.client_id,
        show_window=not args.no_window
    )

    # 运行
    try:
        asyncio.run(visualizer.run(args.duration))
    except KeyboardInterrupt:
        print("\n👋 再见!")


if __name__ == "__main__":
    main()
