#!/usr/bin/env python3
"""
远程服务器测试框架

测试流程：
1. 检查前置条件（ffmpeg, 测试视频, 远程API连接）
2. 启动 ffmpeg 推流到远程服务器
3. 加载任务 (load_task)
4. 启动远程 RTMP 捕获
5. 实时接收视频流并可视化
6. 终止任务和清理

使用方法:
    python integration_tests/remote_test_pipeline.py --server 192.168.1.100 --duration 60
"""

import asyncio
import argparse
import time
import sys
from pathlib import Path
from datetime import datetime

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.utils import FFmpegController, APIClient
from integration_tests.client_viewer import InferenceViewer


class RemoteTestPipeline:
    """远程服务器测试管道"""
    
    def __init__(
        self,
        server_ip: str,
        task_id: int = 1,
        client_id: str = "remote_test_client",
        duration: int = 60,
        video_path: str = None
    ):
        self.server_ip = server_ip
        self.task_id = task_id
        self.client_id = client_id
        self.duration = duration
        
        # 构建URL
        self.api_base_url = f"http://{server_ip}:8000"
        self.rtmp_url = f"rtmp://{server_ip}:1935/live/remote_test"
        
        # 设置测试视频路径
        if video_path is None:
            project_root = Path(__file__).parent.parent
            self.video_path = str(project_root / "test" / "test_video.mp4")
        else:
            self.video_path = video_path
        
        # 初始化控制器
        self.ffmpeg = FFmpegController(self.video_path, self.rtmp_url)
        self.api = APIClient(base_url=self.api_base_url)
        self.show_visualization = True
    
    def run(self) -> bool:
        """运行完整测试"""
        print(f"🌐 远程测试 - 服务器 {self.server_ip} - 时长 {self.duration}s")
        
        try:
            self._check_prerequisites()
            self._start_ffmpeg()
            time.sleep(10)  # 等待推流稳定
            self._load_task()
            self._start_rtmp_capture()
            asyncio.run(self._run_inference_test())
            
            return True
        
        except KeyboardInterrupt:
            print("\n⚠️ 用户中断")
            return False
        except Exception as e:
            print(f"\n❌ 测试失败: {e}")
            return False
        finally:
            self._cleanup()
    
    def _check_prerequisites(self):
        """检查前置条件"""
        # 检查 ffmpeg
        self.ffmpeg._find_ffmpeg()
        
        # 检查测试视频
        if not Path(self.video_path).exists():
            raise Exception(f"测试视频不存在: {self.video_path}")
        
        # 检查远程API
        try:
            self.api._make_request("GET", "/ai/status")
        except Exception as e:
            raise Exception(f"远程API连接失败: {e}")
    
    def _start_ffmpeg(self):
        """启动 ffmpeg 推流到远程服务器"""
        if not self.ffmpeg.start():
            raise Exception("ffmpeg 推流启动失败")
    
    def _load_task(self):
        """加载任务"""
        try:
            response = self.api._make_request("GET", f"/ai/load_task/{self.task_id}")
            print(f"✅ 任务加载成功: task_id={self.task_id}")
        except Exception as e:
            raise Exception(f"加载任务失败: {e}")
    
    def _start_rtmp_capture(self):
        """启动 RTMP 捕获"""
        try:
            # 服务器端使用本地地址访问 RTMP 流
            local_rtmp_url = f"rtmp://localhost:1935/live/remote_test"
            
            self.api.start_rtmp_capture(
                client_id=self.client_id,
                rtmp_url=local_rtmp_url,
                fps=30
            )
            print(f"✅ RTMP 捕获启动成功: {self.client_id}")
        except Exception as e:
            raise Exception(f"启动 RTMP 捕获失败: {e}")
    
    async def _run_inference_test(self):
        """运行推理测试"""
        # 创建带远程服务器IP的可视化客户端
        viewer = RemoteInferenceViewer(
            server_ip=self.server_ip,
            client_id=self.client_id, 
            show_window=self.show_visualization
        )
        await viewer.connect_and_display(self.duration)
    
    def _cleanup(self):
        """清理资源"""
        try:
            self.api.stop_rtmp_capture(self.client_id)
        except:
            pass
        
        try:
            self.api._make_request("POST", f"/ai/terminate_task/{self.client_id}")
        except:
            pass
        
        self.ffmpeg.stop()


class RemoteInferenceViewer(InferenceViewer):
    """远程推理可视化客户端"""
    
    def __init__(self, server_ip: str, client_id: str, show_window=True):
        super().__init__(client_id, show_window)
        self.server_ip = server_ip
    
    async def connect_and_display(self, duration_seconds=None):
        ws_url = f"ws://{self.server_ip}:8000/ai/video?client_id={self.client_id}"
        print(f"🔗 连接到远程 WebSocket: {ws_url}")
        
        self.start_time = datetime.now()
        
        try:
            import websockets
            async with websockets.connect(ws_url) as websocket:
                print("✅ 远程 WebSocket 连接成功")
                
                end_time = None
                if duration_seconds:
                    from datetime import timedelta
                    end_time = datetime.now() + timedelta(seconds=duration_seconds)
                
                while True:
                    if end_time and datetime.now() > end_time:
                        break
                    
                    message = await websocket.recv()
                    if not await self.process_message(message):
                        break
                
        except Exception as e:
            print(f"❌ 远程 WebSocket 错误: {e}")
        finally:
            if self.show_window:
                import cv2
                cv2.destroyAllWindows()


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="CleanSight 远程服务器测试")
    parser.add_argument("--server", "-s", type=str, required=True,
                       help="远程服务器IP地址")
    parser.add_argument("--task_id", type=int, default=1,
                       help="任务ID (默认: 1)")
    parser.add_argument("--client_id", type=str, default="remote_test_client",
                       help="客户端ID")
    parser.add_argument("--duration", "-d", type=int, default=60,
                       help="测试时长（秒）")
    parser.add_argument("--video_path", type=str, default=None,
                       help="测试视频路径")
    parser.add_argument("--no-window", action="store_true",
                       help="禁用可视化窗口")
    
    args = parser.parse_args()
    
    test = RemoteTestPipeline(
        server_ip=args.server,
        task_id=args.task_id,
        client_id=args.client_id,
        duration=args.duration,
        video_path=args.video_path
    )
    
    test.show_visualization = not args.no_window
    
    success = test.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()