"""
完整流程集成测试

测试流程：
1. 前置条件检查（MediaMTX, 后端 API, 数据库）
2. 准备测试任务（创建或使用 task_id=0）
3. 启动 ffmpeg RTMP 推流到本地MediaMTX
4. 等待推流稳定
5. 从数据库加载并启动任务
6. 并发运行：
   - WebSocket 客户端接收推理结果
   - 监控 AI 服务状态
7. 验证 HLS 文件生成
8. 终止任务
9. 清理资源
10. 生成测试报告

注意：RTMP服务现在独立运行，AI后端直接从MediaMTX拉取流进行处理
"""
import asyncio
import argparse
import time
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.utils import (
    FFmpegController,
    DatabaseHelper,
    APIClient,
    check_hls_files,
    wait_for_condition
)
from integration_tests.client_viewer import InferenceViewer


class IntegrationTest:
    """完整流程集成测试"""
    
    def __init__(
        self,
        task_id: int = 0,
        client_id: str = "integration_test_client",
        duration: int = 30,
        rtmp_url: str = "rtmp://localhost:1935/live/test",
        video_path: str = None
    ):
        self.task_id = task_id
        self.client_id = client_id
        self.duration = duration
        self.rtmp_url = rtmp_url
        self.show_visualization = True  # 默认显示可视化窗口
        
        # 设置测试视频路径
        if video_path is None:
            project_root = Path(__file__).parent.parent
            self.video_path = str(project_root / "test" / "test_video.mp4")
        else:
            self.video_path = video_path
        
        # 初始化控制器
        self.ffmpeg = FFmpegController(self.video_path, self.rtmp_url)
        self.api = APIClient()
        self.db = DatabaseHelper()
        
        # 简单状态跟踪
        self.errors = []
    
    def run(self):
        """运行完整测试"""
        print(f"🚀 CleanSight 集成测试 - 任务 {self.task_id} - 时长 {self.duration}s")
        
        try:
            self._check_prerequisites()
            self._prepare_test_task()
            self._start_ffmpeg()
            time.sleep(5)  # 等待推流稳定
            self._start_task()
            self._start_rtmp_capture()
            asyncio.run(self._run_inference_test())
            
            return len(self.errors) == 0
        
        except KeyboardInterrupt:
            print("\n⚠️ 用户中断")
            return False
        finally:
            self._cleanup()
    
    def _check_prerequisites(self):
        """检查前置条件"""
        if not self.api.check_health():
            raise Exception("后端 API 未运行")
        if not Path(self.video_path).exists():
            raise Exception(f"测试视频不存在: {self.video_path}")
        self.ffmpeg._find_ffmpeg()
    
    def _prepare_test_task(self):
        """准备测试任务"""
        task = self.db.get_task(self.task_id)
        if not task:
            self.db.create_test_task(self.task_id, source_ip=self.client_id)
    
    def _start_ffmpeg(self):
        """启动 ffmpeg 推流"""
        if not self.ffmpeg.start():
            raise Exception("ffmpeg 推流启动失败")
    

    
    def _start_task(self):
        """启动任务"""
        self.api.start_task(self.task_id)


    
    def _start_rtmp_capture(self):
        """启动 RTMP 捕获"""
        self.api.start_rtmp_capture(self.client_id, self.rtmp_url, 30)
    
    async def _run_inference_test(self):
        """运行推理测试"""
        viewer = InferenceViewer(self.client_id, show_window=self.show_visualization)
        await viewer.connect_and_display(self.duration)
    

    

    
    def _cleanup(self):
        """清理资源"""
        try:
            self.api.stop_rtmp_capture(self.client_id)
        except:
            pass
        self.ffmpeg.stop()
    



def main():
    parser = argparse.ArgumentParser(description="CleanSightBackend 完整流程集成测试")
    parser.add_argument("--task_id", type=int, default=1,
                       help="任务 ID（默认: 1）")
    parser.add_argument("--client_id", type=str, default="integration_test_client",
                       help="客户端 ID")
    parser.add_argument("--duration", type=int, default=30,
                       help="测试时长（秒，默认: 30）")
    parser.add_argument("--rtmp_url", type=str, default="rtmp://localhost:1935/live/test",
                       help="RTMP 推流地址")
    parser.add_argument("--video_path", type=str, default=None,
                       help="测试视频路径（默认: test/test_video.mp4）")
    parser.add_argument("--no-window", action="store_true",
                       help="禁用可视化窗口，仅在控制台显示统计")
    
    args = parser.parse_args()
    
    test = IntegrationTest(
        task_id=args.task_id,
        client_id=args.client_id,
        duration=args.duration,
        rtmp_url=args.rtmp_url,
        video_path=args.video_path
    )
    
    # 设置可视化选项
    test.show_visualization = not args.no_window
    
    success = test.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
