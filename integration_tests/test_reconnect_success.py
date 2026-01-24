"""
推流断开重连成功测试

测试场景：
1. 推流15秒
2. 停止推流10秒（模拟短暂断流）
3. 恢复推流15秒
4. 验证自动重连成功

预期结果：
- 5秒后进入重连模式
- 第1-2次重连尝试成功
- 推理服务无缝恢复
"""
import sys
import time
import argparse
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.utils import (
    FFmpegController,
    DatabaseHelper,
    APIClient,
)


class ReconnectSuccessTest:
    """断线重连成功测试"""

    def __init__(
        self,
        task_id: int = 1,
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
        self.push_rtsp_url = None  # 本地推流到服务器
        self.pull_rtsp_url = None  # 服务器拉流地址

    def run(self):
        """运行测试"""
        print("🚀 推流断开重连成功测试")
        print("=" * 60)
        print(f"📍 服务器: {self.server}")
        print("=" * 60)

        try:
            # 1. 检查前置条件
            print("\n[1/7] 检查前置条件...")
            self._check_prerequisites()

            # 2. 准备测试任务
            print("\n[2/7] 准备测试任务...")
            self._prepare_test_task()

            # 3. 启动任务
            print("\n[3/7] 启动后端任务...")
            self._start_task()

            # 4. 第一次推流（15秒）
            print("\n[4/7] 开始第一次推流（15秒）...")
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

            # 5. 中断推流（10秒）
            print("\n[5/7] ⚠️  中断推流（10秒）...")
            self._stop_ffmpeg()
            print("⏱️  等待中... (10秒)")
            print("📋 预期：5秒后进入重连模式，每5秒尝试重连")
            time.sleep(10)

            # 6. 重新连接推流
            print("\n[6/7] 🔄 重新连接推流...")
            self._start_ffmpeg()
            wait_time = 5 if self.server != "localhost" else 3
            print(f"⏱️  等待推流建立（{wait_time}秒）...")
            time.sleep(wait_time)

            # 再推流 15 秒
            print("⏱️  推流中... (15秒)")
            time.sleep(15)

            # 7. 结束测试
            print("\n[7/7] ✅ 测试完成，清理资源...")

        except KeyboardInterrupt:
            print("\n⚠️  用户中断测试")
        except Exception as e:
            print(f"\n❌ 测试失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._cleanup()

        print("\n" + "=" * 60)
        print("✅ 测试结束")
        print("\n请检查后端日志，确认：")
        print("  ✓ RECONNECT MODE 日志")
        print("  ✓ RECONNECT ATTEMPT 日志（1-2次）")
        print("  ✓ RECONNECT SUCCESS 日志")

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
            default_source = f"test.reconnect.{self.task_id}"
            self.db.create_test_task(self.task_id, source_ip=default_source)
        else:
            print(f"✅ 任务 {self.task_id} 已存在")

        # 获取 client_id
        task = self.db.get_task(self.task_id)
        if task and getattr(task, 'source_ip', None):
            self.client_id = str(task.source_ip)
            print(f"✅ Client ID: {self.client_id}")
        else:
            raise Exception("❌ 无法从数据库获取 task.source_ip")

        # 生成 RTSP URL
        # 推流URL：本地推流到服务器RTSP服务器
        self.push_rtsp_url = f"rtsp://{self.server}:8004/live/{self.client_id}"
        # 拉流URL：服务器从自己的RTSP服务器拉流（可以是localhost或外网IP）
        # 对于远程服务器，使用localhost更稳定；对于本地测试，两者都可以
        if self.server == "localhost" or self.server == "127.0.0.1":
            self.pull_rtsp_url = f"rtsp://localhost:8004/live/{self.client_id}"
        else:
            # 远程服务器使用localhost拉流（服务器自己连自己）
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
                self.video_path,
                self.push_rtsp_url,
                protocol="rtsp"
            )

        if not self.ffmpeg.start():
            raise Exception("FFmpeg 推流启动失败")

    def _stop_ffmpeg(self):
        """停止 FFmpeg 推流"""
        if self.ffmpeg:
            self.ffmpeg.stop()

    def _start_rtsp_capture(self):
        """启动 RTSP 捕获"""
        result = self.api.start_rtsp_capture(self.client_id, self.pull_rtsp_url, 30)
        if "error" in result:
            raise Exception(f"启动 RTSP 捕获失败: {result['error']}")
        print(f"✅ RTSP 捕获已启动（后端拉流: {self.pull_rtsp_url}）")

    def _cleanup(self):
        """清理资源"""
        print("\n🧹 清理资源...")

        # 停止推流
        if self.ffmpeg:
            self.ffmpeg.stop()
            print("✅ FFmpeg 已停止")

        # 停止 RTSP 捕获
        if self.client_id:
            try:
                self.api.stop_rtsp_capture(self.client_id)
                print("✅ RTSP 捕获已停止")
            except Exception as e:
                print(f"⚠️  停止 RTSP 捕获失败: {e}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="推流断开重连成功测试")
    parser.add_argument(
        "--task_id",
        type=int,
        default=1,
        help="任务 ID（默认: 1）"
    )
    parser.add_argument(
        "--video_path",
        type=str,
        default=None,
        help="测试视频路径（默认: test/test_video.mp4）"
    )
    parser.add_argument(
        "--server",
        type=str,
        default="localhost",
        help="服务器地址（默认: localhost，远程示例: 117.50.241.174）"
    )

    args = parser.parse_args()

    test = ReconnectSuccessTest(
        task_id=args.task_id,
        video_path=args.video_path,
        server=args.server,
    )

    test.run()


if __name__ == "__main__":
    main()
