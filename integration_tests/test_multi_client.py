"""
多客户端并发集成测试（Scenario 6）

从数据库查询最多 max-tasks 个任务，并发运行 scenario 1（正常流程）。
观测走 admin 运维面板（http://{server}:{api_port}/admin-f3m8/ui/ 「总览」/「实时监控」tab）。

用法:
    python integration_tests/test_multi_client.py [options]

参数:
    --server      <host>    服务器地址（默认: localhost）
    --duration    <seconds> 运行时长（默认: 60）
    --max-tasks   <int>     最大并发任务数（默认: 5）
    --video_path  <path>    测试视频路径（默认: test/test_video.mp4）
    --fps         <int>     推流帧率（默认: 30）
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func

from app.database import get_db
from app.models import DBTask


def get_test_tasks(max_tasks: int) -> list:
    """
    从数据库查询最多 max_tasks 个任务，每个 source_ip 取一个（task_id 最小的）。
    返回 [(task_id, source_ip), ...]。
    """
    db = next(get_db())
    try:
        subq = (
            db.query(DBTask.source_ip, func.min(DBTask.task_id).label("min_task_id"))
            .group_by(DBTask.source_ip)
            .subquery()
        )
        tasks = (
            db.query(DBTask)
            .join(
                subq,
                (DBTask.source_ip == subq.c.source_ip) & (DBTask.task_id == subq.c.min_task_id),
            )
            .limit(max_tasks)
            .all()
        )
        return [(t.task_id, str(t.source_ip)) for t in tasks]
    finally:
        db.close()


def spawn_worker(task_id: int, args, log_dir: Path):
    """
    启动单个 test_single_client.py --scenario 1 子进程。
    返回 (subprocess.Popen, log_path)。

    端口与阶段**必须透传**：测试环境常做端口偏移（如 8100/8104），阶段决定路由到哪个
    推理 workflow。漏传会让子进程默默打到默认端口 / 默认 LEAK 阶段，与单客户端跑法不等价。
    """
    log_path = log_dir / f"multi_task_{task_id}_{int(time.time())}.log"
    cmd = [
        sys.executable,
        str(Path(__file__).parent / "test_single_client.py"),
        "--scenario", "1",
        "--task_id", str(task_id),
        "--server", args.server,
        "--api-port", str(args.api_port),
        "--rtsp-port", str(args.rtsp_port),
        "--duration", str(args.duration),
        "--video_path", args.video_path,
        "--fps", str(args.fps),
    ]
    if args.current_step is not None:
        cmd += ["--current-step", str(args.current_step)]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    log_file = open(log_path, "w", encoding="utf-8")

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).parent.parent),
        env=env,
    )
    return proc, log_path, log_file


def monitor_and_wait(procs: list, task_ids: list, log_paths: list):
    """轮询直到所有子进程结束，每 10s 打印一次状态。"""
    done = set()
    start = time.time()
    while len(done) < len(procs):
        for i, proc in enumerate(procs):
            if i not in done and proc.poll() is not None:
                elapsed = int(time.time() - start)
                status = "OK" if proc.returncode == 0 else f"FAIL (rc={proc.returncode})"
                print(f"[{elapsed}s] Task {task_ids[i]} 完成: {status}")
                if proc.returncode != 0:
                    print(f"  查看日志: {log_paths[i]}")
                done.add(i)
        if len(done) < len(procs):
            elapsed = int(time.time() - start)
            running = len(procs) - len(done)
            print(f"[{elapsed}s] 运行中: {running} 个任务...")
            time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="CleanSight 多客户端并发集成测试（Scenario 6）")
    parser.add_argument("--server", default="localhost", help="服务器地址（默认: localhost）")
    parser.add_argument("--duration", type=int, default=60, help="运行时长（秒，默认: 60）")
    parser.add_argument("--max-tasks", type=int, default=5, dest="max_tasks", help="最大并发任务数（默认: 5）")
    parser.add_argument("--video_path", default=None, help="测试视频路径（默认: test/test_video.mp4）")
    parser.add_argument("--fps", type=int, default=30, help="推流帧率（默认: 30）")
    parser.add_argument("--api-port", type=int, default=8000, dest="api_port", help="后端 API 端口（默认: 8000）")
    parser.add_argument("--rtsp-port", type=int, default=8004, dest="rtsp_port", help="RTSPProxy 推流端口（默认: 8004）")
    parser.add_argument(
        "--current-step", default=None, dest="current_step",
        help="任务 current_step（1=LEAK / 2=CLEAN / 其它=MOCK）；透传给每个子进程。"
             "复用已存在任务时须与 DB 中的值一致（子进程会 fail-fast）。",
    )
    parser.add_argument(
        "--task-ids", default=None, dest="task_ids",
        help="逗号分隔的 task_id 列表（如 119,120,121）。指定则不查 DB 自动挑选；"
             "不存在的任务由子进程自建（source_ip=test.s{task_id}，结束自动清理）。",
    )
    args = parser.parse_args()

    if args.video_path is None:
        args.video_path = str(Path(__file__).parent.parent / "test" / "test_video.mp4")

    if not Path(args.video_path).exists():
        raise SystemExit(f"测试视频不存在: {args.video_path}")

    print("=" * 60)
    print("CleanSight 多客户端并发集成测试")
    print(f"  服务器: {args.server}")
    print(f"  最大任务数: {args.max_tasks}")
    print(f"  运行时长: {args.duration}s")
    print("=" * 60)

    if args.task_ids:
        # 显式指定：每路一个 task_id，子进程自建缺失的任务。
        # source_ip 是推流路径（rtsp://…/live/{source_ip}）与后端路由键，**每路必须不同**——
        # 自建任务用 test.s{task_id} 天然互异；复用真实任务时须自行确认不撞。
        ids = [int(x) for x in args.task_ids.split(",") if x.strip()]
        tasks = [(tid, "(子进程解析)") for tid in ids]
    else:
        tasks = get_test_tasks(args.max_tasks)
    if not tasks:
        raise SystemExit("数据库中没有找到任务，请先创建任务")

    print(f"\n找到 {len(tasks)} 个任务: {[t[0] for t in tasks]}")

    print(f"\n观测走 admin 运维面板（后端自带，同源同端口）:")
    print(f"  http://{args.server}:{args.api_port}/admin-f3m8/ui/")
    print(f"  → 「总览」看各客户端队列/健康，「实时监控」逐个选客户端看画面\n")

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    procs, task_ids, log_paths, log_files = [], [], [], []
    try:
        for task_id, source_ip in tasks:
            proc, log_path, log_file = spawn_worker(task_id, args, log_dir)
            procs.append(proc)
            task_ids.append(task_id)
            log_paths.append(log_path)
            log_files.append(log_file)
            print(f"启动 Task {task_id} ({source_ip}) PID={proc.pid} → {log_path.name}")
            time.sleep(2)  # 错开启动，避免同时争抢 MediaMTX

        print(f"\n已启动 {len(procs)} 个子进程，等待完成...\n")
        monitor_and_wait(procs, task_ids, log_paths)

    except KeyboardInterrupt:
        print("\n用户中断，终止所有子进程...")
        for p in procs:
            if p.poll() is None:
                p.terminate()
        time.sleep(2)
        for p in procs:
            if p.poll() is None:
                p.kill()
    finally:
        for lf in log_files:
            try:
                lf.close()
            except Exception:
                pass

    passed = sum(1 for p in procs if p.poll() == 0)
    failed = len(procs) - passed

    print("\n" + "=" * 60)
    print(f"测试结果: {passed}/{len(procs)} 通过，{failed} 失败")
    if log_paths:
        print("详细日志:")
        for i, lp in enumerate(log_paths):
            status = "OK" if procs[i].poll() == 0 else "FAIL"
            print(f"  [{status}] {lp}")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
