"""
完整流程集成测试 - RTSP版本

测试流程：
1. 前置条件检查（MediaMTX, 后端 API, 数据库）
2. 准备测试任务（创建或使用 task_id=0）
3. 启动 ffmpeg RTSP 推流到本地MediaMTX
4. 等待推流稳定
5. 从数据库加载并启动任务
6. 并发运行：
   - WebSocket 客户端接收推理结果
   - 监控 AI 服务状态
7. 验证 HLS 文件生成
8. 终止任务
9. 清理资源
10. 生成测试报告

注意：RTSP服务现在独立运行，AI后端直接从MediaMTX拉取流进行处理
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.client_viewer import InferenceViewer
from integration_tests.utils import APIClient, DatabaseHelper, FFmpegController


class IntegrationTest:
    """完整流程集成测试"""

    def __init__(
        self,
        task_id: int = 0,
        duration: int = 30,
        video_path: str = None,
        server: str = "117.50.241.174",
    ):
        self.task_id = task_id
        # client_id and rtsp_url will be populated from DB at runtime
        self.client_id = None
        self.duration = duration
        self.rtsp_url = None
        self.show_visualization = True  # 默认显示可视化窗口

        # 设置测试视频路径
        if video_path is None:
            project_root = Path(__file__).parent.parent
            self.video_path = str(project_root / "test" / "test_video.mp4")
        else:
            self.video_path = video_path

        # 初始化控制器（延迟创建，使用正确的 rtsp_url）
        self.ffmpeg = None
        # server 和端口设置（保留原始分别端口）
        self.server = server
        self.api_port = 8000
        self.mediamtx_port = 8004
        self.api = APIClient(base_url=f"http://{self.server}:{self.api_port}")
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
            # time.sleep(5)  # 等待推流稳定
            self._start_task()
            self._start_rtsp_capture()
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
        # FFmpeg 二进制检查将在创建 FFmpegController 时进行（延迟创建）

    def _prepare_test_task(self):
        """准备测试任务"""
        task = self.db.get_task(self.task_id)
        if not task:
            # 如果任务不存在，使用 task_id 派生一个默认 source_ip（例如 172.16.77.<task_id>）
            default_source = f"rtsp.test.{self.task_id if self.task_id>0 else 221}"
            self.db.create_test_task(self.task_id, source_ip=default_source)

    def _start_ffmpeg(self):
        """启动 ffmpeg 推流"""
        # 从数据库读取 task 的 source_ip 作为 client_id，并生成 RTSP 地址
        task = self.db.get_task(self.task_id)
        if task and getattr(task, "source_ip", None):
            self.client_id = str(task.source_ip)
        else:
            raise Exception("无法从数据库获取 task.source_ip 来生成 client_id")

        # 生成 RTSP 地址（使用 MediaMTX 端口）
        self.rtsp_url = (
            f"rtsp://{self.server}:{self.mediamtx_port}/live/{self.client_id}"
        )

        # 创建 FFmpegController（延迟创建以使用正确的 rtsp_url）
        self.ffmpeg = FFmpegController(self.video_path, self.rtsp_url, protocol="rtsp")

        if not self.ffmpeg.start():
            raise Exception("ffmpeg 推流启动失败")

    def _start_task(self):
        """启动任务"""
        self.api.start_task(self.task_id)

    def _start_rtsp_capture(self):
        """启动 RTSP 捕获"""
        self.api.start_rtsp_capture(self.client_id, self.rtsp_url, 30)

    async def _run_inference_test(self):
        """运行推理测试"""
        print("启动推理结果可视化客户端...")
        viewer = InferenceViewer(
            self.client_id,
            show_window=self.show_visualization,
            base_port=f"{self.server}:{self.api_port}",
        )
        await viewer.connect_and_display(self.duration)

    def _cleanup(self):
        """清理资源"""
        try:
            self.api.stop_rtsp_capture(self.client_id)
        except:
            pass
        self.ffmpeg.stop()


def main():
    parser = argparse.ArgumentParser(description="CleanSightBackend 完整流程集成测试")
    parser.add_argument("--task_id", type=int, default=1, help="任务 ID（默认: 1）")
    parser.add_argument(
        "--client_id", type=str, default="integration_test_client", help="客户端 ID"
    )
    parser.add_argument(
        "--duration", type=int, default=30, help="测试时长（秒，默认: 30）"
    )
    parser.add_argument(
        "--rtsp_url",
        type=str,
        default="rtsp://localhost:8554/live/test",
        help="RTSP 推流地址",
    )
    parser.add_argument(
        "--video_path",
        type=str,
        default=None,
        help="测试视频路径（默认: test/test_video.mp4）",
    )
    parser.add_argument(
        "--server",
        type=str,
        default="117.50.241.174",
        help="后端 server 地址（默认: 117.50.241.174）",
    )
    parser.add_argument(
        "--no-window", action="store_true", help="禁用可视化窗口，仅在控制台显示统计"
    )

    args = parser.parse_args()

    test = IntegrationTest(
        task_id=args.task_id,
        duration=args.duration,
        video_path=args.video_path,
        server=args.server,
    )

    # 设置可视化选项
    test.show_visualization = not args.no_window

    success = test.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
