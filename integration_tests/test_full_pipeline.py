"""
完整流程集成测试

测试流程：
1. 前置条件检查（MediaMTX, 后端 API, 数据库）
2. 准备测试任务（创建或使用 task_id=0）
3. 启动 ffmpeg RTMP 推流
4. 启动后端 RTMP 捕获
5. 从数据库加载并启动任务
6. 并发运行：
   - WebSocket 客户端接收推理结果
   - 监控 AI 服务状态
7. 验证 HLS 文件生成
8. 终止任务
9. 清理资源
10. 生成测试报告
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
        
        # 测试结果
        self.results = {
            "start_time": None,
            "end_time": None,
            "ffmpeg_started": False,
            "rtmp_capture_started": False,
            "task_started": False,
            "frames_received": 0,
            "hls_files": {},
            "task_terminated": False,
            "errors": []
        }
    
    def run(self):
        """运行完整测试"""
        print("=" * 70)
        print("🚀 CleanSightBackend 集成测试")
        print("=" * 70)
        print(f"任务 ID: {self.task_id}")
        print(f"客户端 ID: {self.client_id}")
        print(f"测试时长: {self.duration} 秒")
        print(f"RTMP URL: {self.rtmp_url}")
        print(f"测试视频: {self.video_path}")
        print("=" * 70)
        
        self.results["start_time"] = datetime.now()
        
        try:
            # 步骤 1: 前置条件检查
            if not self._check_prerequisites():
                self._print_report()
                return False
            
            # 步骤 2: 准备测试任务
            if not self._prepare_test_task():
                self._print_report()
                return False
            
            # 步骤 3: 启动 ffmpeg 推流
            if not self._start_ffmpeg():
                self._print_report()
                return False
            
            # 步骤 4: 启动后端 RTMP 捕获
            if not self._start_rtmp_capture():
                self._cleanup()
                self._print_report()
                return False
            
            # 步骤 5: 启动任务
            if not self._start_task():
                self._cleanup()
                self._print_report()
                return False
            
            # 步骤 6: 运行测试（接收推理结果）
            asyncio.run(self._run_inference_test())
            
            # 步骤 7: 验证 HLS 文件
            self._verify_hls_files()
            
            # 步骤 8: 终止任务
            self._terminate_task()
            
            # 步骤 9: 清理资源
            self._cleanup()
            
            self.results["end_time"] = datetime.now()
            
            # 步骤 10: 生成报告
            self._print_report()
            
            return len(self.results["errors"]) == 0
        
        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断测试")
            self._cleanup()
            self._print_report()
            return False
        
        except Exception as e:
            print(f"\n❌ 测试执行异常: {e}")
            import traceback
            traceback.print_exc()
            self.results["errors"].append(f"执行异常: {str(e)}")
            self._cleanup()
            self._print_report()
            return False
    
    def _check_prerequisites(self) -> bool:
        """检查前置条件"""
        print("\n📋 步骤 1: 检查前置条件")
        print("-" * 70)
        
        success = True
        
        # 检查 ffmpeg
        try:
            self.ffmpeg._find_ffmpeg()
            print(f"✅ ffmpeg: {self.ffmpeg.ffmpeg_path}")
        except Exception as e:
            print(f"❌ ffmpeg 未找到: {e}")
            self.results["errors"].append("ffmpeg 未安装")
            success = False
        
        # 检查测试视频
        if not Path(self.video_path).exists():
            print(f"❌ 测试视频不存在: {self.video_path}")
            self.results["errors"].append(f"测试视频不存在: {self.video_path}")
            success = False
        else:
            print(f"✅ 测试视频: {self.video_path}")
        
        # 检查后端 API
        if self.api.check_health():
            print("✅ 后端 API: http://localhost:8000")
        else:
            print("❌ 后端 API 无法连接")
            print("   请先启动后端: uvicorn app.main:app --reload")
            self.results["errors"].append("后端 API 未运行")
            success = False
        
        # 检查数据库连接
        try:
            from app.database import get_db
            db = next(get_db())
            db.close()
            print("✅ 数据库连接正常")
        except Exception as e:
            print(f"❌ 数据库连接失败: {e}")
            self.results["errors"].append(f"数据库连接失败: {str(e)}")
            success = False
        
        # MediaMTX 检查（尝试推流会验证）
        print("⏳ MediaMTX 将在推流时验证...")
        
        return success
    
    def _prepare_test_task(self) -> bool:
        """准备测试任务"""
        print(f"\n📋 步骤 2: 准备测试任务 (task_id={self.task_id})")
        print("-" * 70)
        
        # 检查任务是否存在
        task = self.db.get_task(self.task_id)
        if task:
            print(f"✅ 任务 {self.task_id} 已存在")
            print(f"   状态: {task.status}")
            print(f"   当前步骤: {task.current_step}")
            print(f"   客户端 IP: {task.source_ip}")
        else:
            print(f"⏳ 任务 {self.task_id} 不存在，创建中...")
            # 使用 self.client_id 作为 source_ip 以保持一致性
            if not self.db.create_test_task(self.task_id, source_ip=self.client_id):
                self.results["errors"].append(f"创建任务 {self.task_id} 失败")
                return False
            print(f"   已创建任务，客户端 ID: {self.client_id}")
        
        return True
    
    def _start_ffmpeg(self) -> bool:
        """启动 ffmpeg 推流"""
        print(f"\n📋 步骤 3: 启动 ffmpeg 推流")
        print("-" * 70)
        
        if self.ffmpeg.start():
            self.results["ffmpeg_started"] = True
            print("⏳ 等待推流稳定 (8 秒)...")
            time.sleep(8)
            return True
        else:
            self.results["errors"].append("ffmpeg 推流启动失败")
            return False
    
    def _start_rtmp_capture(self) -> bool:
        """启动后端 RTMP 捕获"""
        print(f"\n📋 步骤 4: 启动后端 RTMP 捕获")
        print("-" * 70)
        
        try:
            result = self.api.start_rtmp_capture(self.client_id, self.rtmp_url)
            print(f"✅ RTMP 捕获已启动: {result}")
            self.results["rtmp_capture_started"] = True
            
            print("⏳ 等待捕获初始化 (5 秒)...")
            time.sleep(5)
            return True
        except Exception as e:
            print(f"❌ 启动 RTMP 捕获失败: {e}")
            self.results["errors"].append(f"启动 RTMP 捕获失败: {str(e)}")
            return False
    
    def _start_task(self) -> bool:
        """从数据库加载并启动任务"""
        print(f"\n📋 步骤 5: 启动任务 (task_id={self.task_id})")
        print("-" * 70)
        
        try:
            result = self.api.start_task(self.task_id)
            print(f"✅ 任务已启动: {result}")
            self.results["task_started"] = True
            
            # 验证任务状态
            task = self.db.get_task(self.task_id)
            if task:
                print(f"   数据库状态: {task.status}")
                print(f"   开始时间: {task.start_time}")
            
            return True
        except Exception as e:
            print(f"❌ 启动任务失败: {e}")
            self.results["errors"].append(f"启动任务失败: {str(e)}")
            return False
    
    async def _run_inference_test(self):
        """运行推理测试（接收 WebSocket 结果）"""
        print(f"\n📋 步骤 6: 接收推理结果 ({self.duration} 秒)")
        print("-" * 70)
        
        viewer = InferenceViewer(self.client_id)
        await viewer.connect_and_display(self.duration)
        
        self.results["frames_received"] = viewer.frame_count
    
    def _verify_hls_files(self):
        """验证 HLS 文件生成"""
        print(f"\n📋 步骤 7: 验证 HLS 文件生成")
        print("-" * 70)
        
        hls_info = check_hls_files(self.client_id, self.task_id)
        self.results["hls_files"] = hls_info
        
        if hls_info["exists"]:
            print(f"✅ HLS 目录存在: {hls_info['path']}")
            print(f"   视频段数量: {len(hls_info['segments'])}")
            print(f"   关键点文件: {len(hls_info['keypoints'])}")
            print(f"   播放列表: {len(hls_info['playlists'])}")
            
            if len(hls_info['segments']) > 0:
                print(f"   示例视频段: {Path(hls_info['segments'][0]).name}")
        else:
            print(f"⚠️ HLS 目录不存在: {hls_info['path']}")
            print("   可能原因：")
            print("   1. 测试时长太短，未生成段")
            print("   2. 帧捕获未成功")
            print("   3. 路径配置问题")
    
    def _terminate_task(self):
        """终止任务"""
        print(f"\n📋 步骤 8: 终止任务")
        print("-" * 70)
        
        try:
            # 从数据库读取任务的 source_ip 作为 client_id
            task = self.db.get_task(self.task_id)
            if not task:
                raise Exception(f"任务 {self.task_id} 不存在")
            
            client_id = task.source_ip
            print(f"   使用客户端 ID: {client_id}")
            
            # 使用 client_id 调用终止接口
            result = self.api.terminate_task(client_id)
            print(f"✅ 任务已终止: {result}")
            self.results["task_terminated"] = True
            
            # 验证数据库状态
            self.db.session = None  # 强制重新查询
            task = self.db.get_task(self.task_id)
            if task:
                print(f"   数据库状态: {task.status}")
                print(f"   结束时间: {task.end_time}")
        except Exception as e:
            print(f"❌ 终止任务失败: {e}")
            self.results["errors"].append(f"终止任务失败: {str(e)}")
    
    def _cleanup(self):
        """清理资源"""
        print(f"\n📋 步骤 9: 清理资源")
        print("-" * 70)
        
        # 停止 RTMP 捕获
        if self.results["rtmp_capture_started"]:
            try:
                self.api.stop_rtmp_capture(self.client_id)
                print(f"✅ 已停止 RTMP 捕获: {self.client_id}")
            except Exception as e:
                print(f"⚠️ 停止 RTMP 捕获失败: {e}")
        
        # 停止 ffmpeg
        if self.results["ffmpeg_started"]:
            self.ffmpeg.stop()
        
        print("✅ 资源清理完成")
    
    def _print_report(self):
        """生成测试报告"""
        print("\n" + "=" * 70)
        print("📊 集成测试报告")
        print("=" * 70)
        
        if self.results["start_time"] and self.results["end_time"]:
            duration = (self.results["end_time"] - self.results["start_time"]).total_seconds()
            print(f"测试时长: {duration:.1f} 秒")
        
        print(f"\n✅ 成功步骤:")
        if self.results["ffmpeg_started"]:
            print("  - ffmpeg 推流启动")
        if self.results["rtmp_capture_started"]:
            print("  - RTMP 捕获启动")
        if self.results["task_started"]:
            print("  - 任务启动")
        if self.results["frames_received"] > 0:
            print(f"  - 接收推理结果 ({self.results['frames_received']} 帧)")
        if self.results["hls_files"].get("exists"):
            print(f"  - HLS 文件生成 ({len(self.results['hls_files']['segments'])} 段)")
        if self.results["task_terminated"]:
            print("  - 任务终止")
        
        if self.results["errors"]:
            print(f"\n❌ 错误 ({len(self.results['errors'])}):")
            for error in self.results["errors"]:
                print(f"  - {error}")
        
        print("\n" + "=" * 70)
        
        if len(self.results["errors"]) == 0:
            print("🎉 测试通过！")
        else:
            print("⚠️ 测试失败，请检查错误信息")
        
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="CleanSightBackend 完整流程集成测试")
    parser.add_argument("--task_id", type=int, default=0,
                       help="任务 ID（默认: 0）")
    parser.add_argument("--client_id", type=str, default="integration_test_client",
                       help="客户端 ID")
    parser.add_argument("--duration", type=int, default=30,
                       help="测试时长（秒，默认: 30）")
    parser.add_argument("--rtmp_url", type=str, default="rtmp://localhost:1935/live/test",
                       help="RTMP 推流地址")
    parser.add_argument("--video_path", type=str, default=None,
                       help="测试视频路径（默认: test/test_video.mp4）")
    
    args = parser.parse_args()
    
    test = IntegrationTest(
        task_id=args.task_id,
        client_id=args.client_id,
        duration=args.duration,
        rtmp_url=args.rtmp_url,
        video_path=args.video_path
    )
    
    success = test.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
