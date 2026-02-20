"""
本地完整流程集成测试 - 使用统一 API

使用新的统一 API 接口：
- POST /api/start: 一步完成加载任务 + 启动流
- POST /api/terminate: 完整清理所有资源

测试流程：
1. 前置条件检查（MediaMTX, 后端 API, 数据库）
2. 准备测试任务（创建或使用 task_id）
3. 启动 ffmpeg RTSP 推流到本地MediaMTX
4. 等待推流稳定
5. 使用统一 API 启动（自动加载任务 + 启动流）
6. 并发运行：WebSocket 客户端接收推理结果
7. 使用统一 API 终止（完整清理所有资源）
8. 清理 FFmpeg
9. 生成测试报告
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.client_viewer import InferenceViewer
from integration_tests.utils import APIClient, DatabaseHelper, FFmpegController


class UnifiedAPILocalTest:
    """使用统一 API 的本地集成测试"""

    def __init__(
        self,
        task_id: int = 1,
        duration: int = 30,
        video_path: str = None,
        show_window: bool = True,
    ):
        self.task_id = task_id
        self.client_id = None
        self.duration = duration
        self.rtsp_url = None
        self.show_window = show_window

        # 设置测试视频路径
        if video_path is None:
            project_root = Path(__file__).parent.parent
            self.video_path = str(project_root / "test" / "test_video.mp4")
        else:
            self.video_path = video_path

        # 初始化控制器
        self.ffmpeg = None
        self.api = APIClient(base_url="http://localhost:8000")
        self.db = DatabaseHelper()

        # 状态跟踪
        self.errors = []
        self.start_time = None
        self.end_time = None

    def run(self):
        """运行完整测试"""
        print("=" * 70)
        print(
            f"🚀 本地集成测试（统一 API） - 任务 {self.task_id} - 时长 {self.duration}s"
        )
        print("=" * 70)

        try:
            self._check_prerequisites()
            self._prepare_test_task()
            self._start_ffmpeg()
            time.sleep(5)  # 等待推流稳定

            # 使用新统一 API：一步完成
            self._unified_start()

            # 运行推理测试
            asyncio.run(self._run_inference_test())

            return len(self.errors) == 0

        except KeyboardInterrupt:
            print("\n⚠️ 用户中断")
            return False
        except Exception as e:
            print(f"\n❌ 测试失败: {e}")
            import traceback

            traceback.print_exc()
            self.errors.append(str(e))
            return False
        finally:
            self._cleanup()
            self._print_report()

    def _check_prerequisites(self):
        """检查前置条件"""
        print("\n📋 检查前置条件...")

        if not self.api.check_health():
            raise Exception("后端 API 未运行 (http://localhost:8000)")
        print("  ✅ 后端 API 运行中")

        if not Path(self.video_path).exists():
            raise Exception(f"测试视频不存在: {self.video_path}")
        print(f"  ✅ 测试视频存在: {self.video_path}")

    def _prepare_test_task(self):
        """准备测试任务"""
        print("\n📝 准备测试任务...")

        task = self.db.get_task(self.task_id)
        if not task:
            default_source = f"local.unified.{self.task_id}"
            self.db.create_test_task(self.task_id, source_ip=default_source)
            task = self.db.get_task(self.task_id)
            print(f"  ✅ 创建测试任务: task_id={self.task_id}")

        if task and getattr(task, "source_ip", None):
            self.client_id = str(task.source_ip)
        else:
            raise Exception("无法从数据库获取 task.source_ip")

        # 生成 RTSP 地址（本地 MediaMTX 端口 8004）
        self.rtsp_url = f"rtsp://localhost:8004/live/{self.client_id}"
        print(f"  ✅ client_id: {self.client_id}")
        print(f"  ✅ rtsp_url: {self.rtsp_url}")

    def _start_ffmpeg(self):
        """启动 ffmpeg 推流"""
        print("\n🎬 启动 FFmpeg 推流...")

        self.ffmpeg = FFmpegController(self.video_path, self.rtsp_url, protocol="rtsp")
        if not self.ffmpeg.start():
            raise Exception("FFmpeg 推流启动失败")

        print(f"  ✅ FFmpeg 推流已启动")
        print(
            f"     进程 PID: {self.ffmpeg.process.pid if self.ffmpeg.process else 'N/A'}"
        )

    def _unified_start(self):
        """使用新统一 API 启动"""
        print("\n🚀 使用统一 API 启动...")
        print(f"   接口: POST /api/start")
        print(f"   参数: task_id={self.task_id}, rtsp_url={self.rtsp_url}, fps=30")

        self.start_time = time.time()

        result = self.api.unified_start(
            task_id=self.task_id, rtsp_url=self.rtsp_url, fps=30
        )

        if "error" in result:
            raise Exception(f"启动失败: {result['error']}")

        print(f"  ✅ 启动成功")
        print(f"     状态: {result.get('status')}")
        print(f"     客户端: {result.get('client_id')}")
        print(f"     任务ID: {result.get('task_id')}")
        print(f"     消息: {result.get('message')}")
        print(f"\n  📝 统一 API 自动完成:")
        print(f"     ✓ 从数据库加载任务配置")
        print(f"     ✓ 检测跨任务切换（如有）")
        print(f"     ✓ 设置任务到 AI 服务")
        print(f"     ✓ 启动 RTSP 流捕获")

    async def _run_inference_test(self):
        """运行推理测试"""
        print(f"\n🎥 启动推理结果监控 ({self.duration}s)...")

        viewer = InferenceViewer(
            self.client_id, show_window=self.show_window, base_port="localhost:8000"
        )

        await viewer.connect_and_display(self.duration)

    def _cleanup(self):
        """清理资源"""
        print("\n🧹 清理资源...")

        # 使用新统一 API 终止
        if self.client_id:
            print(f"   接口: POST /api/terminate?client_id={self.client_id}")

            result = self.api.unified_terminate(self.client_id)

            self.end_time = time.time()

            if "error" in result:
                print(f"  ⚠️ 终止失败: {result['error']}")
                self.errors.append(f"terminate: {result['error']}")
            else:
                print(f"  ✅ 终止成功")
                print(f"     状态: {result.get('status')}")

                if result.get("cleanup_details"):
                    details = result["cleanup_details"]
                    print(f"\n  📝 清理详情:")
                    print(
                        f"     解码器停止: {'✓' if details.get('decoder_stopped') else '✗'}"
                    )
                    print(
                        f"     数据落盘: {'✓' if details.get('data_flushed') else '✗'}"
                    )
                    print(
                        f"     ClientManager清理: {'✓' if details.get('client_cleaned') else '✗'}"
                    )

                    if details.get("errors"):
                        print(f"     ⚠️ 错误: {details['errors']}")
                        self.errors.extend(details["errors"])

        # 停止 FFmpeg
        if self.ffmpeg:
            print(f"\n  停止 FFmpeg 推流...")
            self.ffmpeg.stop()
            print(f"     ✓ FFmpeg 已停止")

    def _print_report(self):
        """打印测试报告"""
        print("\n" + "=" * 70)
        print("📊 测试报告")
        print("=" * 70)

        if self.start_time and self.end_time:
            duration = self.end_time - self.start_time
            print(f"测试时长: {duration:.1f}s")

        print(f"任务ID: {self.task_id}")
        print(f"客户端ID: {self.client_id}")
        print(f"RTSP URL: {self.rtsp_url}")
        print(f"推理监控时长: {self.duration}s")

        if self.errors:
            print(f"\n⚠️ 错误数量: {len(self.errors)}")
            for i, error in enumerate(self.errors, 1):
                print(f"  {i}. {error}")
        else:
            print(f"\n✅ 测试通过，无错误")

        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="本地集成测试 - 统一 API")
    parser.add_argument("--task_id", type=int, default=1, help="任务 ID（默认: 1）")
    parser.add_argument(
        "--duration", type=int, default=30, help="测试时长（秒，默认: 30）"
    )
    parser.add_argument("--video_path", type=str, default=None, help="测试视频路径")
    parser.add_argument("--no-window", action="store_true", help="禁用可视化窗口")

    args = parser.parse_args()

    test = UnifiedAPILocalTest(
        task_id=args.task_id,
        duration=args.duration,
        video_path=args.video_path,
        show_window=not args.no_window,
    )

    success = test.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
