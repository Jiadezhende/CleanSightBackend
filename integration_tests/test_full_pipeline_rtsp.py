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
import asyncio
import argparse
import time
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

import requests

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.utils import (
    FFmpegController,
    DatabaseHelper,
    APIClient,
)
from integration_tests.client_viewer import InferenceViewer


class IntegrationTest:
    """完整流程集成测试"""
    
    def __init__(
        self,
        task_id: int = 0,
        duration: int = 30,
        video_path: str = None # type: ignore
    ):
        self.task_id = task_id
        # client_id will be populated from task.source_ip at runtime
        self.client_id: Optional[str] = None
        self.duration = duration
        # RTSP 地址将在启动 ffmpeg 时根据数据库中 task.source_ip 自动构建
        self.rtsp_url: Optional[str] = None
        self.show_visualization = True  # 默认显示可视化窗口
        
        # 设置测试视频路径
        if video_path is None:
            project_root = Path(__file__).parent.parent
            self.video_path = str(project_root / "test" / "leak_test.mp4")
        else:
            self.video_path = video_path
        
        # FFmpegController 将在启动时根据实际 rtsp_url 创建
        self.ffmpeg: Optional[FFmpegController] = None
        self.api = APIClient()
        # 仍保留 DatabaseHelper 引用，以便必要时本地调试；
        # 但默认优先通过远端脚本接口获取任务信息。
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
            print("加载任务")
            self._start_task()
            self._start_rtsp_capture()
            print("开始推理测试客户端")
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
        # self.ffmpeg._find_ffmpeg()  # 延迟到启动时创建 ffmpeg 实例时检查
    
    def _fetch_task_info_from_script(self) -> Optional[Dict[str, Any]]:
        """通过远端脚本接口获取任务信息（CI 数据）。

        使用文档中的接口:
        http://116.204.65.72:8881/gdmp/v1/api/nt/get_task_information?task_id=X
        """
        url = "http://116.204.65.72:8881/gdmp/v1/api/nt/get_task_information"
        try:
            resp = requests.get(url, params={"task_id": self.task_id}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"❌ 通过脚本接口获取任务信息失败: {e}")
            return None

        if not isinstance(data, dict):
            print(f"❌ 脚本接口返回非 JSON 结构: {data}")
            return None

        if data.get("code") != 0 or not isinstance(data.get("data"), dict):
            print(f"❌ 脚本接口返回错误: {data}")
            return None

        return data["data"]

    def _prepare_test_task(self):
        """准备测试任务: 优先通过脚本接口获取任务信息, 失败则回退到本地数据库。"""
        info = self._fetch_task_info_from_script()

        # 1. 优先使用脚本接口返回的任务信息（CI 环境）
        if info and info.get("source_ip"):
            self.client_id = str(info["source_ip"])
            print(f"✅ 从脚本获取任务信息成功: task_id={self.task_id}, client_id={self.client_id}")
            return

        # 2. 如果脚本接口返回 404 或其它错误, 回退到本地数据库逻辑
        print(f"⚠️ 脚本接口未返回有效任务信息, 回退到本地数据库: task_id={self.task_id}")

        # 先尝试从本地数据库获取任务
        task = self.db.get_task(self.task_id)
        if not task:
            # 若不存在则创建一个测试任务
            fallback_client_id = self.client_id or f"rtsp_integration_{self.task_id}"
            if not self.db.create_test_task(self.task_id, source_ip=fallback_client_id):
                raise Exception(f"本地数据库无法创建测试任务 {self.task_id}")
            task = self.db.get_task(self.task_id)

        if not task or not getattr(task, "source_ip", None):
            raise Exception(f"本地数据库中任务 {self.task_id} 缺少 source_ip 字段")

        # 使用本地任务的 source_ip 作为 client_id
        self.client_id = str(task.source_ip)
        print(f"✅ 使用本地数据库任务: task_id={self.task_id}, client_id={self.client_id}")
    
    def _start_ffmpeg(self):
        """启动 ffmpeg 推流"""
        # 若尚未从脚本侧拿到 client_id，则再尝试获取一次
        if not self.client_id:
            info = self._fetch_task_info_from_script()
            if info and info.get("source_ip"):
                self.client_id = str(info["source_ip"])
            else:
                # 兜底：退回本地数据库查询
                task = self.db.get_task(self.task_id)
                if task and getattr(task, 'source_ip', None):
                    self.client_id = str(task.source_ip)

        if not self.client_id:
            raise Exception(f"无法获取任务 {self.task_id} 的 client_id/source_ip")
        # 生成 RTSP 地址
        self.rtsp_url = f"rtsp://localhost:8004/live/{self.client_id}"

        # 创建 FFmpegController（延迟创建以使用正确的 rtsp_url）
        self.ffmpeg = FFmpegController(self.video_path, self.rtsp_url, protocol="rtsp")
        
        self.ffmpeg._find_ffmpeg()

        if not self.ffmpeg.start():
            raise Exception("ffmpeg 推流启动失败")
    

    
    def _start_task(self):
        """启动任务"""
        self.api.start_task(self.task_id)


    
    def _start_rtsp_capture(self):
        """启动 RTSP 捕获"""
        self.api.start_rtsp_capture(self.client_id, self.rtsp_url, 30) # type: ignore
    
    async def _run_inference_test(self):
        """运行推理测试"""
        print("启动推理结果可视化客户端...")
        viewer = InferenceViewer(self.client_id, show_window=self.show_visualization)
        await viewer.connect_and_display(self.duration)
    

    

    
    def _cleanup(self):
        """清理资源"""
        try:
            self.api.stop_rtsp_capture(self.client_id) # type: ignore
        except:
            pass
        self.ffmpeg.stop() # type: ignore


def main():
    parser = argparse.ArgumentParser(description="CleanSightBackend 完整流程集成测试")
    parser.add_argument("--task_id", type=int, default=1,
                       help="任务 ID（默认: 1）")
    # 不再需要提供 client_id，测试会根据 task_id 从数据库读取 source_ip 自动生成
    parser.add_argument("--duration", type=int, default=30,
                       help="测试时长（秒，默认: 30）")
    # 不再需要手动提供 rtsp_url，测试会根据 task_id 从数据库读取 source_ip 自动生成
    parser.add_argument("--video_path", type=str, default=None,
                       help="测试视频路径（默认: test/test_video.mp4）")
    parser.add_argument("--no-window", action="store_true",
                       help="禁用可视化窗口，仅在控制台显示统计")
    
    args = parser.parse_args()
    
    test = IntegrationTest(
        task_id=args.task_id,
        duration=args.duration,
        video_path=args.video_path
    )
    
    # 设置可视化选项
    test.show_visualization = not args.no_window
    
    success = test.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
