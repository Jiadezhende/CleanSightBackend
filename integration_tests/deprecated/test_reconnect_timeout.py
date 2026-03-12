"""
推流断开超时清理测试

测试场景：
1. 推流15秒
2. 停止推流（不再恢复）
3. 观察35秒，等待6次重连尝试后自动清理

预期结果：
- 5秒后进入重连模式
- 每5秒尝试重连一次（共6次）
- 30秒后（6次失败）自动清理资源
"""

import argparse
import sys
import time
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.utils import APIClient, DatabaseHelper, FFmpegController


class ReconnectTimeoutTest:
    """推流断开超时清理测试"""

    def __init__(
        self,
        task_id: int = 2,
        video_path: str = None,
        server: str = "localhost",
    ):
        self.task_id = task_id
        self.server = server
        self.client_id = None

        # 设置测试视频路径
        if video_path is None:
            project_root = Path(__file__).parent.parent
            self.video_path = str(project_root / "test" / "test_video.mp4")
        else:
            self.video_path = video_path

        # 初始化控制器
        self.api = APIClient(base_url=f"http://{server}:8000")
        self.db = DatabaseHelper()
        self.ffmpeg = None

        # RTSP URL
        self.push_rtsp_url = None
        self.pull_rtsp_url = None

    def run(self):
        """运行测试"""
        print("🚀 推流断开超时清理测试")
        print("=" * 60)
        print(f"📍 服务器: {self.server}")
        print("📋 测试场景：")
        print("   1. 推流 15 秒")
        print("   2. 停止推流（不恢复）")
        print("   3. 等待 35 秒观察重连尝试和自动清理")
        print("=" * 60)

        try:
            # 1. 检查前置条件
            print("\n[1/5] 检查前置条件...")
            self._check_prerequisites()

            # 2. 准备测试任务
            print("\n[2/5] 准备测试任务...")
            self._prepare_test_task()

            # 3. 启动任务
            print("\n[3/5] 启动后端任务...")
            self._start_task()

            # 4. 推流 15 秒
            print("\n[4/5] 开始推流（15秒）...")
            self._start_ffmpeg()
            wait_time = 5 if self.server != "localhost" else 3
            print(f"⏱️  等待推流建立（{wait_time}秒）...")
            time.sleep(wait_time)

            # 启动 RTSP 捕获
            print("📡 启动 RTSP 捕获...")
            self._start_rtsp_capture()

            # 推流 15 秒
            print("⏱️  推流中... (15秒)")
            time.sleep(15)

            # 5. 停止推流并观察重连
            print("\n[5/5] ⚠️  停止推流（不恢复），观察自动重连...")
            self._stop_ffmpeg()

            print("\n" + "=" * 60)
            print("🔍 观察期（35秒）")
            print("预期日志：")
            print("  - 00:05 - 进入重连模式")
            print("  - 00:05 - 第1次重连尝试")
            print("  - 00:10 - 第2次重连尝试")
            print("  - 00:15 - 第3次重连尝试")
            print("  - 00:20 - 第4次重连尝试")
            print("  - 00:25 - 第5次重连尝试")
            print("  - 00:30 - 第6次重连尝试")
            print("  - 00:30 - 重连失败，自动清理资源")
            print("=" * 60)

            # 等待观察期（35秒，留5秒余量）
            for i in range(35):
                remaining = 35 - i
                print(f"\r⏱️  观察中... ({remaining}秒)", end="", flush=True)
                time.sleep(1)

            print("\n\n✅ 观察期结束")

            # 验证资源已清理
            print("\n🔍 验证资源清理状态...")
            self._verify_cleanup()

            print("\n" + "=" * 60)
            print("✅ 测试完成")
            print("\n请检查后端日志，确认：")
            print("  ✓ RECONNECT MODE 日志")
            print("  ✓ 6次 RECONNECT ATTEMPT 日志")
            print("  ✓ RECONNECT FAILED 日志")
            print("  ✓ CleanupService 自动清理日志")

        except KeyboardInterrupt:
            print("\n⚠️  用户中断测试")
        except Exception as e:
            print(f"\n❌ 测试失败: {e}")
            import traceback

            traceback.print_exc()
        finally:
            self._cleanup()

        print("=" * 60)

    def _check_prerequisites(self):
        """检查前置条件"""
        # 检查后端 API
        if not self.api.check_health():
            raise Exception(f"❌ 后端 API 未运行: {self.server}:8000")
        print(f"✅ 后端 API 正常: {self.server}:8000")

        # 检查测试视频
        if not Path(self.video_path).exists():
            raise Exception(f"❌ 测试视频不存在: {self.video_path}")
        print(f"✅ 测试视频存在: {self.video_path}")

    def _prepare_test_task(self):
        """准备测试任务"""
        task = self.db.get_task(self.task_id)
        if not task:
            print(f"⚠️  任务 {self.task_id} 不存在，创建新任务...")
            default_source = f"test.timeout.{self.task_id}"
            self.db.create_test_task(self.task_id, source_ip=default_source)
        else:
            print(f"✅ 任务 {self.task_id} 已存在")

        # 获取 client_id
        task = self.db.get_task(self.task_id)
        if task and getattr(task, "source_ip", None):
            self.client_id = str(task.source_ip)
            print(f"✅ Client ID: {self.client_id}")
        else:
            raise Exception("❌ 无法从数据库获取 task.source_ip")

        # 生成 RTSP URL
        self.push_rtsp_url = f"rtsp://{self.server}:8004/live/{self.client_id}"

        if self.server == "localhost" or self.server == "127.0.0.1":
            self.pull_rtsp_url = f"rtsp://localhost:8004/live/{self.client_id}"
        else:
            self.pull_rtsp_url = f"rtsp://localhost:8004/live/{self.client_id}"

        print(f"✅ 推流URL（本地→服务器）: {self.push_rtsp_url}")
        print(f"✅ 拉流URL（服务器拉流）: {self.pull_rtsp_url}")

    def _start_task(self):
        """启动任务"""
        result = self.api.start_task(self.task_id)
        if "error" in result:
            raise Exception(f"启动任务失败: {result['error']}")
        print(f"✅ 任务 {self.task_id} 已加载到 AI 服务")

    def _start_ffmpeg(self):
        """启动 FFmpeg 推流"""
        if self.ffmpeg is None:
            self.ffmpeg = FFmpegController(
                self.video_path, self.push_rtsp_url, protocol="rtsp"
            )

        if not self.ffmpeg.start():
            raise Exception("FFmpeg 推流启动失败")

    def _stop_ffmpeg(self):
        """停止 FFmpeg 推流"""
        if self.ffmpeg:
            self.ffmpeg.stop()
            print("✅ FFmpeg 已停止（不再恢复）")

    def _start_rtsp_capture(self):
        """启动 RTSP 捕获"""
        result = self.api.start_rtsp_capture(self.client_id, self.pull_rtsp_url, 30)
        if "error" in result:
            raise Exception(f"启动 RTSP 捕获失败: {result['error']}")
        print(f"✅ RTSP 捕获已启动（后端拉流: {self.pull_rtsp_url}）")

    def _verify_cleanup(self):
        """验证资源已清理"""
        try:
            # 检查流状态（应该已经被清理）
            result = self.api.get_stream_status()

            # 如果client_id不在活动列表中，说明已清理
            if result and "clients" in result:
                if self.client_id not in result["clients"]:
                    print(f"✅ 资源已清理：{self.client_id} 不在活动流列表中")
                else:
                    print(f"⚠️  资源可能未清理：{self.client_id} 仍在活动流列表中")
            else:
                print("⚠️  无法验证清理状态（API返回异常）")

        except Exception as e:
            print(f"⚠️  验证清理状态失败: {e}")

    def _cleanup(self):
        """清理资源"""
        print("\n🧹 清理资源...")

        # 停止推流
        if self.ffmpeg:
            self.ffmpeg.stop()
            print("✅ FFmpeg 已停止")

        # 注意：不主动调用stop_rtsp_capture，让后端自动清理完成
        # 如果自动清理成功，资源应该已经释放


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="推流断开超时清理测试")
    parser.add_argument(
        "--task_id", type=int, default=2, help="任务 ID（默认: 2，避免与重连测试冲突）"
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
        default="localhost",
        help="服务器地址（默认: localhost，远程示例: 117.50.241.174）",
    )

    args = parser.parse_args()

    test = ReconnectTimeoutTest(
        task_id=args.task_id,
        video_path=args.video_path,
        server=args.server,
    )

    test.run()


if __name__ == "__main__":
    main()
