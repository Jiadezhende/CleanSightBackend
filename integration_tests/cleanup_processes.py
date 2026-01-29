"""
清理后台推流进程的脚本
用于清理stress_test.py启动的残留进程
"""
import subprocess
import sys
import psutil
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def find_and_kill_test_processes():
    """查找并终止测试相关的进程"""
    killed_count = 0

    # 需要查找的进程关键字
    keywords = [
        'remote_full_pipeline_rtsp.py',
        'stress_test.py',
        'ffmpeg',
    ]

    logger.info("正在查找测试相关进程...")

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            # 获取进程信息
            pid = proc.info['pid']
            name = proc.info['name']
            cmdline = proc.info['cmdline']

            # 跳过当前脚本进程
            if cmdline and 'cleanup_processes.py' in ' '.join(cmdline):
                continue

            # 检查是否匹配关键字
            cmdline_str = ' '.join(cmdline) if cmdline else ''
            should_kill = False

            for keyword in keywords:
                if keyword in cmdline_str or keyword in name:
                    should_kill = True
                    break

            if should_kill:
                logger.info(f"找到进程 - PID: {pid}, 名称: {name}")
                logger.info(f"  命令行: {cmdline_str[:100]}...")

                # 尝试终止进程
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                    logger.info(f"✅ 已终止进程 PID: {pid}")
                    killed_count += 1
                except psutil.TimeoutExpired:
                    logger.warning(f"⚠️ 进程 {pid} 未响应，强制终止")
                    proc.kill()
                    proc.wait()
                    logger.info(f"✅ 已强制终止进程 PID: {pid}")
                    killed_count += 1
                except Exception as e:
                    logger.error(f"❌ 终止进程 {pid} 失败: {e}")

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    return killed_count

def main():
    logger.info("=" * 60)
    logger.info("清理后台推流进程")
    logger.info("=" * 60)

    try:
        count = find_and_kill_test_processes()

        logger.info("\n" + "=" * 60)
        if count > 0:
            logger.info(f"✅ 成功清理 {count} 个进程")
        else:
            logger.info("✅ 没有发现需要清理的进程")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.error(f"\n❌ 清理失败: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
