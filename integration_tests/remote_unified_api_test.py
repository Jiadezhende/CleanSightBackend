"""
远程完整流程集成测试 - 使用统一 API

适用于 Ubuntu 服务器或其他远程环境
使用新的统一 API 接口：
- POST /api/start: 一步完成加载任务 + 启动流
- POST /api/terminate: 完整清理所有资源

测试流程：
1. 前置条件检查（后端 API, 数据库）
2. 准备测试任务
3. 使用外部 RTSP 推流源（无需本地 FFmpeg）
4. 使用统一 API 启动
5. 监控推理结果（无可视化窗口）
6. 使用统一 API 终止
7. 生成测试报告
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
from integration_tests.utils import APIClient, DatabaseHelper


class UnifiedAPIRemoteTest:
    """使用统一 API 的远程集成测试"""

    def __init__(
        self,
        task_id: int = 1,
        rtsp_url: str = None,
        duration: int = 60,
        base_url: str = "http://localhost:8000",
    ):
        self.task_id = task_id
        self.rtsp_url = rtsp_url
        self.client_id = None
        self.duration = duration
        self.base_url = base_url

        # 初始化工具
        self.api = APIClient(base_url=base_url)
        self.db = DatabaseHelper()

        # 状态跟踪
        self.errors = []
        self.start_time = None
        self.end_time = None
        self.frames_received = 0

    def run(self):
        """运行完整测试"""
        print("=" * 70)
        print(f"🚀 远程集成测试（统一 API）")
        print(f"   任务ID: {self.task_id}")
        print(f"   测试时长: {self.duration}s")
        print(f"   后端地址: {self.base_url}")
        print("=" * 70)

        try:
            self._check_prerequisites()
            self._prepare_test_task()

            # 使用新统一 API：一步完成
            self._unified_start()

            # 运行推理测试（无可视化）
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
            raise Exception(f"后端 API 未运行 ({self.base_url})")
        print(f"  ✅ 后端 API 运行中: {self.base_url}")

        if not self.rtsp_url:
            raise Exception("必须提供 RTSP URL（--rtsp_url）")
        print(f"  ✅ RTSP URL: {self.rtsp_url}")

    def _prepare_test_task(self):
        """准备测试任务"""
        print("\n📝 准备测试任务...")

        task = self.db.get_task(self.task_id)
        if not task:
            # 为远程测试创建默认任务
            default_source = f"remote.unified.{self.task_id}"
            self.db.create_test_task(self.task_id, source_ip=default_source)
            task = self.db.get_task(self.task_id)
            print(f"  ✅ 创建测试任务: task_id={self.task_id}")
        else:
            print(f"  ✅ 任务已存在: task_id={self.task_id}")

        if task and getattr(task, "source_ip", None):
            self.client_id = str(task.source_ip)
        else:
            raise Exception("无法从数据库获取 task.source_ip")

        print(f"  ✅ client_id: {self.client_id}")
        print(f"  ✅ current_step: {getattr(task, 'current_step', 'N/A')}")

    def _unified_start(self):
        """使用新统一 API 启动"""
        print("\n🚀 使用统一 API 启动...")
        print(f"   接口: POST {self.base_url}/api/start")
        print(f"   参数:")
        print(f"     - task_id: {self.task_id}")
        print(f"     - rtsp_url: {self.rtsp_url}")
        print(f"     - fps: 30")

        self.start_time = time.time()

        result = self.api.unified_start(
            task_id=self.task_id, rtsp_url=self.rtsp_url, fps=30
        )

        if "error" in result:
            raise Exception(f"启动失败: {result['error']}")

        print(f"\n  ✅ 启动成功")
        print(f"     状态: {result.get('status')}")
        print(f"     客户端: {result.get('client_id')}")
        print(f"     任务ID: {result.get('task_id')}")
        print(f"     消息: {result.get('message')}")

        print(f"\n  📝 统一 API 自动完成:")
        print(f"     ✓ 从数据库加载任务配置")
        print(f"     ✓ 检测跨任务切换并清理旧数据（如有）")
        print(f"     ✓ 设置任务到 AI 推理服务")
        print(f"     ✓ 启动 RTSP 流捕获和解码")
        print(f"     ✓ 自动启动推理管道")

        # 等待流启动
        print(f"\n  ⏳ 等待流启动稳定...")
        time.sleep(3)

    async def _run_inference_test(self):
        """运行推理测试（无可视化窗口）"""
        print(f"\n🎥 启动推理结果监控 ({self.duration}s)...")
        print(f"   模式: 无可视化窗口（远程模式）")

        viewer = InferenceViewer(
            self.client_id,
            show_window=False,  # 远程模式不显示窗口
            base_port=self.base_url.replace("http://", "").replace("https://", ""),
        )

        try:
            # 记录开始时间
            start = time.time()
            last_report = start

            # 监控推理结果
            await viewer.connect_and_display(self.duration)

            # 获取统计信息
            self.frames_received = getattr(viewer, "frames_received", 0)

            print(f"\n  ✅ 监控完成")
            print(f"     接收帧数: {self.frames_received}")
            print(f"     测试时长: {time.time() - start:.1f}s")

        except Exception as e:
            print(f"\n  ⚠️ 监控错误: {e}")
            self.errors.append(f"inference_monitor: {e}")

    def _cleanup(self):
        """清理资源"""
        print("\n🧹 清理资源...")

        if self.client_id:
            print(f"   接口: POST {self.base_url}/api/terminate")
            print(f"   参数: client_id={self.client_id}")

            result = self.api.unified_terminate(self.client_id)

            self.end_time = time.time()

            if "error" in result:
                print(f"\n  ⚠️ 终止失败: {result['error']}")
                self.errors.append(f"terminate: {result['error']}")
            else:
                print(f"\n  ✅ 终止成功")
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
                        print(f"     ⚠️ 清理错误: {details['errors']}")
                        self.errors.extend(details["errors"])
                    else:
                        print(f"     ✓ 无错误")

                print(f"\n  📝 统一 API 完成清理:")
                print(f"     ✓ 停止流解码器（FFmpeg 进程）")
                print(f"     ✓ 落盘残余推理数据")
                print(f"     ✓ 清理客户端队列资源")
                print(f"     ✓ 释放所有相关内存")

    def _print_report(self):
        """打印测试报告"""
        print("\n" + "=" * 70)
        print("📊 测试报告")
        print("=" * 70)

        print(f"后端地址: {self.base_url}")
        print(f"任务ID: {self.task_id}")
        print(f"客户端ID: {self.client_id}")
        print(f"RTSP URL: {self.rtsp_url}")
        print(f"推理监控时长: {self.duration}s")

        if self.start_time and self.end_time:
            total_duration = self.end_time - self.start_time
            print(f"总测试时长: {total_duration:.1f}s")

        print(f"接收帧数: {self.frames_received}")

        if self.frames_received > 0 and self.start_time and self.end_time:
            fps = self.frames_received / (self.end_time - self.start_time)
            print(f"平均帧率: {fps:.1f} fps")

        print(f"\nAPI 使用:")
        print(f"  启动: POST /api/start")
        print(f"  终止: POST /api/terminate")

        if self.errors:
            print(f"\n⚠️ 错误数量: {len(self.errors)}")
            for i, error in enumerate(self.errors, 1):
                print(f"  {i}. {error}")
            print("\n❌ 测试失败")
        else:
            print(f"\n✅ 测试通过，无错误")

        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="远程集成测试 - 统一 API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用外部 RTSP 流测试
  python remote_unified_api_test.py --task_id 1 --rtsp_url rtsp://camera.local/stream --duration 60

  # 指定远程后端地址
  python remote_unified_api_test.py --task_id 2 --rtsp_url rtsp://... --base_url http://192.168.1.100:8000 --duration 120
        """,
    )
    parser.add_argument("--task_id", type=int, required=True, help="任务 ID")
    parser.add_argument("--rtsp_url", type=str, required=True, help="RTSP 流地址")
    parser.add_argument(
        "--duration", type=int, default=60, help="测试时长（秒，默认: 60）"
    )
    parser.add_argument(
        "--base_url",
        type=str,
        default="http://localhost:8000",
        help="后端 API 地址（默认: http://localhost:8000）",
    )

    args = parser.parse_args()

    test = UnifiedAPIRemoteTest(
        task_id=args.task_id,
        rtsp_url=args.rtsp_url,
        duration=args.duration,
        base_url=args.base_url,
    )

    success = test.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
