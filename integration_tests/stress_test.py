"""
压力测试脚本 - 并发推流测试

功能：
1. 从远程数据库获取最多10个不同source_id的任务
2. 创建并行进程，同时向远程服务器推流
3. 只有第一个进程显示可视化窗口，其他进程使用--no-window

使用方法：
    python integration_tests/stress_test.py --duration 60 --server 117.50.241.174
    参数说明：
    --duration: 测试时长（秒），默认60秒
    --server: 远程服务器地址，默认
    --max-tasks: 最大并发任务数，默认10

    进程清理脚本：python integration_tests/cleanup_processes.py
"""
import os
import sys
import subprocess
import argparse
import time
from pathlib import Path
from typing import List, Tuple
from multiprocessing import Process, Queue
import logging

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_db
from app.models.task import DBTask
from sqlalchemy import func

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(process)d] - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class StressTestCoordinator:
    """压力测试协调器"""

    def __init__(
        self,
        duration: int = 60,
        server: str = "117.50.241.174",
        max_tasks: int = 10,
        video_path: str = None,
    ):
        self.duration = duration
        self.server = server
        self.max_tasks = max_tasks

        # 设置测试视频路径
        if video_path is None:
            project_root = Path(__file__).parent.parent
            self.video_path = str(project_root / "test" / "test_video.mp4")
        else:
            self.video_path = video_path

        # 验证视频文件存在
        if not Path(self.video_path).exists():
            raise FileNotFoundError(f"测试视频不存在: {self.video_path}")

        self.processes: List[subprocess.Popen] = []
        self.task_ids: List[int] = []

    def get_test_tasks(self) -> List[Tuple[int, str]]:
        """从数据库获取最多10个不同source_id的任务

        Returns:
            List[Tuple[int, str]]: (task_id, source_ip) 列表
        """
        db = next(get_db())
        try:
            # 查询不同source_ip的任务，最多10个
            # 使用子查询找出每个source_ip的最小task_id
            subquery = (
                db.query(
                    DBTask.source_ip,
                    func.min(DBTask.task_id).label('min_task_id')
                )
                .group_by(DBTask.source_ip)
                .subquery()
            )

            # 关联查询获取完整任务信息
            tasks = (
                db.query(DBTask)
                .join(
                    subquery,
                    (DBTask.source_ip == subquery.c.source_ip) &
                    (DBTask.task_id == subquery.c.min_task_id)
                )
                .limit(self.max_tasks)
                .all()
            )

            result = [(task.task_id, task.source_ip) for task in tasks]

            logger.info(f"从数据库获取到 {len(result)} 个不同source_id的任务")
            for task_id, source_ip in result:
                logger.info(f"  - Task ID: {task_id}, Source IP: {source_ip}")

            return result

        except Exception as e:
            logger.error(f"获取任务失败: {e}")
            return []
        finally:
            db.close()

    def start_test_process(self, task_id: int, show_window: bool = False) -> subprocess.Popen:
        """启动单个测试进程

        Args:
            task_id: 任务ID
            show_window: 是否显示可视化窗口

        Returns:
            subprocess.Popen: 进程对象
        """
        # 构建命令
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "remote_full_pipeline_rtsp.py"),
            "--task_id", str(task_id),
            "--duration", str(self.duration),
            "--server", self.server,
            "--video_path", self.video_path,
        ]

        # 只有第一个进程不添加--no-window
        if not show_window:
            cmd.append("--no-window")

        # 启动进程
        try:
            # 创建日志文件路径
            log_dir = Path(__file__).parent / "logs"
            log_dir.mkdir(exist_ok=True)

            log_file_path = log_dir / f"stress_test_task_{task_id}_{int(time.time())}.log"
            log_file = open(log_file_path, 'w', encoding='utf-8')

            # 设置环境变量，确保子进程使用UTF-8编码
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'

            # 不使用CREATE_NEW_CONSOLE，将输出重定向到日志文件
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,  # 合并stderr到stdout
                cwd=str(Path(__file__).parent.parent),  # 设置工作目录为项目根目录
                env=env,  # 传递环境变量
            )

            status = "带可视化窗口" if show_window else "无窗口模式"
            logger.info(f"启动测试进程 - Task ID: {task_id}, PID: {process.pid} ({status})")
            logger.info(f"  日志文件: {log_file_path}")

            # 保存日志文件引用和路径
            if not hasattr(self, 'log_files'):
                self.log_files = []
                self.log_paths = []
            self.log_files.append(log_file)
            self.log_paths.append(log_file_path)

            return process

        except Exception as e:
            logger.error(f"启动进程失败 - Task ID: {task_id}, 错误: {e}")
            return None

    def run(self) -> bool:
        """运行压力测试

        Returns:
            bool: 测试是否成功
        """
        logger.info("=" * 80)
        logger.info(f"🚀 CleanSight 压力测试启动")
        logger.info(f"   测试时长: {self.duration} 秒")
        logger.info(f"   远程服务器: {self.server}")
        logger.info(f"   最大并发任务数: {self.max_tasks}")
        logger.info(f"   测试视频: {self.video_path}")
        logger.info("=" * 80)

        # 获取测试任务
        test_tasks = self.get_test_tasks()

        if not test_tasks:
            logger.error("❌ 未获取到测试任务，退出")
            return False

        logger.info(f"\n准备启动 {len(test_tasks)} 个并发测试进程...")

        # 启动所有测试进程
        try:
            for idx, (task_id, _) in enumerate(test_tasks):
                # 只有第一个进程显示窗口
                show_window = (idx == 0)

                process = self.start_test_process(task_id, show_window)
                if process:
                    self.processes.append(process)
                    self.task_ids.append(task_id)

                # 稍微延迟，避免同时启动导致资源争抢
                time.sleep(2)
        except KeyboardInterrupt:
            logger.warning("\n⚠️ 启动阶段被中断")
            raise

        if not self.processes:
            logger.error("❌ 所有进程启动失败")
            return False

        logger.info(f"\n✅ 已启动 {len(self.processes)} 个测试进程")
        logger.info("=" * 80)

        # 监控进程状态
        try:
            self._monitor_processes()
            return True

        except KeyboardInterrupt:
            logger.warning("\n⚠️ 用户中断测试")
            return False

        finally:
            self._cleanup()

    def _monitor_processes(self):
        """监控所有进程的状态"""
        start_time = time.time()
        completed_count = 0
        failed_indices = set()

        logger.info("开始监控进程状态...\n")

        while True:
            # 检查所有进程状态
            running_count = 0
            failed_count = 0

            for idx, process in enumerate(self.processes):
                if process.poll() is None:
                    running_count += 1
                else:
                    # 进程已结束
                    if process.returncode == 0:
                        if idx not in failed_indices:
                            logger.info(f"✅ Task {self.task_ids[idx]} (PID: {process.pid}) 完成")
                            completed_count += 1
                            failed_indices.add(idx)  # 标记已处理
                    else:
                        if idx not in failed_indices:
                            logger.error(f"❌ Task {self.task_ids[idx]} (PID: {process.pid}) 失败 (退出码: {process.returncode})")
                            if hasattr(self, 'log_paths') and idx < len(self.log_paths):
                                logger.error(f"   查看日志: {self.log_paths[idx]}")
                            failed_count += 1
                            failed_indices.add(idx)  # 标记已处理

            # 所有进程都结束了
            if running_count == 0:
                logger.info("\n" + "=" * 80)
                logger.info("所有测试进程已完成")
                logger.info(f"  成功: {completed_count}")
                logger.info(f"  失败: {len(failed_indices) - completed_count}")
                logger.info("=" * 80)
                break

            # 显示运行状态
            elapsed = int(time.time() - start_time)
            logger.info(f"[{elapsed}s] 运行中: {running_count}, 已完成: {completed_count}, 失败: {len(failed_indices) - completed_count}")

            # 等待一段时间再检查
            time.sleep(10)

    def _cleanup(self):
        """清理资源，终止所有子进程"""
        logger.info("\n正在清理资源...")

        if not self.processes:
            logger.info("没有需要清理的进程")
            return

        # 首先尝试正常终止所有进程
        for idx, process in enumerate(self.processes):
            if process and process.poll() is None:
                try:
                    task_id = self.task_ids[idx] if idx < len(self.task_ids) else "未知"
                    logger.info(f"终止进程 - Task ID: {task_id}, PID: {process.pid}")
                    process.terminate()
                except Exception as e:
                    logger.error(f"发送终止信号失败 - PID: {process.pid}, 错误: {e}")

        # 等待所有进程终止
        logger.info("等待进程终止...")
        time.sleep(2)

        # 检查并强制终止仍在运行的进程
        for idx, process in enumerate(self.processes):
            if process and process.poll() is None:
                try:
                    task_id = self.task_ids[idx] if idx < len(self.task_ids) else "未知"
                    logger.warning(f"强制终止进程 - Task ID: {task_id}, PID: {process.pid}")
                    process.kill()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        logger.error(f"无法终止进程 - PID: {process.pid}")
                except Exception as e:
                    logger.error(f"强制终止失败 - 错误: {e}")

        # 关闭日志文件
        if hasattr(self, 'log_files'):
            for log_file in self.log_files:
                try:
                    log_file.close()
                except:
                    pass

        logger.info("清理完成")

        # 打印日志文件位置
        if hasattr(self, 'log_paths'):
            logger.info("\n查看详细日志:")
            for log_path in self.log_paths:
                logger.info(f"  - {log_path}")


def main():
    parser = argparse.ArgumentParser(description="CleanSight 压力测试脚本")
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="测试时长（秒，默认: 60）"
    )
    parser.add_argument(
        "--server",
        type=str,
        default="117.50.241.174",
        help="远程服务器地址（默认: 117.50.241.174）"
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=10,
        help="最大并发任务数（默认: 10）"
    )
    parser.add_argument(
        "--video-path",
        type=str,
        default=None,
        help="测试视频路径（默认: test/test_video.mp4）"
    )

    args = parser.parse_args()

    try:
        coordinator = StressTestCoordinator(
            duration=args.duration,
            server=args.server,
            max_tasks=args.max_tasks,
            video_path=args.video_path,
        )

        success = coordinator.run()
        sys.exit(0 if success else 1)

    except Exception as e:
        logger.error(f"压力测试失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
