"""
单客户端集成测试 - 覆盖5种使用场景

用法:
    python integration_tests/test_single_client.py --scenario <1-5> --task_id <id> [options]

场景:
    1 - 正常流程:   推流 → start → 等待 → terminate
    2 - 断流重连(成功): 推流 → start → 断流 → 重连 → terminate
    3 - 断流重连(失败): 推流 → start → 断流 → 等待自动清理
    4 - 仅推流:     推流，不调用任何 API
    5 - 仅 start:   调 start 但不推流(no-stream) 或 不调 terminate(no-terminate)

参数:
    --scenario    {1,2,3,4,5}         必填
    --server      <host>               默认 localhost
    --task_id     <int>                必填
    --duration    <seconds>            默认 60
    --video_path  <path>               默认 test/test_video.mp4
    --fps         <int>                默认 30
    --no-window                        禁用 OpenCV 可视化窗口
    --mode        no-stream|no-terminate  仅 scenario 5，默认 no-stream
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.client_viewer import InferenceViewer
from integration_tests.utils import APIClient, DatabaseHelper, FFmpegController

# 自动清理超时: heartbeat(5s) + max_attempts(5) × interval(5s) + buffer(10s) = 40s
AUTO_CLEANUP_TIMEOUT = 45
# 断流重连成功场景: gap 必须 < max_attempts×interval = 25s
RECONNECT_GAP = 10


# ---------------------------------------------------------------------------
# 共享帮助函数
# ---------------------------------------------------------------------------


def build_urls(server: str, client_id: str) -> tuple:
    """
    返回 (push_url, pull_url)。
    push_url: FFmpeg 推流目标（用服务器 IP，远程时为服务器公网地址）
    pull_url: /api/start 传的 RTSP 地址（后端始终从自己的 localhost 拉流）
    """
    push_url = f"rtsp://{server}:8004/live/{client_id}"
    pull_url = f"rtsp://localhost:8004/live/{client_id}"
    return push_url, pull_url


def ensure_task(task_id: int) -> str:
    """确保任务存在于数据库，返回 client_id（即 task.source_ip）。"""
    db = DatabaseHelper()
    task = db.get_task(task_id)
    if not task:
        source_ip = f"test.s{task_id}"
        print(f"任务 {task_id} 不存在，创建新任务 (source_ip={source_ip})")
        db.create_test_task(task_id, source_ip=source_ip)
        task = db.get_task(task_id)
    client_id = str(task.source_ip)
    print(f"client_id: {client_id}")
    return client_id


def check_prerequisites(api: APIClient, video_path: str):
    """检查后端健康状态和测试视频是否存在。"""
    if not api.check_health():
        raise SystemExit("后端 API 不可达，请先启动后端服务")
    print(f"后端 API 正常: {api.base_url}")
    if not Path(video_path).exists():
        raise SystemExit(f"测试视频不存在: {video_path}")
    print(f"测试视频: {video_path}")


def stream_stabilize(ffmpeg: FFmpegController, is_remote: bool):
    """启动 FFmpeg 推流并等待稳定。"""
    if not ffmpeg.start():
        raise RuntimeError("FFmpeg 推流启动失败")
    wait = 5 if is_remote else 3
    print(f"等待推流稳定 ({wait}s)...")
    time.sleep(wait)


def poll_until_cleaned(api: APIClient, client_id: str, timeout: int = AUTO_CLEANUP_TIMEOUT) -> bool:
    """
    轮询 GET /health/status，直到 client_id 不再出现在 queues 中（说明已自动清理）。
    返回 True 表示已确认清理，False 表示超时。
    """
    deadline = time.time() + timeout
    print(f"等待后端自动清理 {client_id}（最多 {timeout}s）...")
    while time.time() < deadline:
        status = api._make_request("GET", "/health/status")
        queues = status.get("queues", {})
        reconnecting = status.get("monitor_stats", {}).get("reconnecting_clients", [])
        remaining = int(deadline - time.time())
        print(f"  [{remaining}s 剩余] 已知客户端: {list(queues.keys())}, 重连中: {reconnecting}")
        if client_id not in queues:
            print(f"已确认: {client_id} 已从系统中清理")
            return True
        time.sleep(3)
    print(f"超时: {client_id} 在 {timeout}s 内未被自动清理")
    return False


def print_viewer_url(server: str, client_id: str):
    """打印浏览器查看器 URL（--no-window 模式的替代方案）。"""
    viewer_path = Path(__file__).parent / "viewer.html"
    print(f"\n如需在浏览器中查看推理结果，请打开:")
    print(f"  file:///{viewer_path}?client_id={client_id}&server={server}:8000")
    print(f"  或运行: python -m http.server 8080")
    print(f"  然后访问: http://localhost:8080/integration_tests/viewer.html?client_id={client_id}&server={server}:8000\n")


# ---------------------------------------------------------------------------
# Scenario 1: 正常流程
# ---------------------------------------------------------------------------


def run_scenario_1(args):
    """
    正常使用流程：推流 → start → 等待 duration → terminate。
    验证: 完整的推理可视化、后端正常响应、terminate 成功清理资源。
    """
    print("\n" + "=" * 60)
    print("Scenario 1: 正常流程")
    print("=" * 60)

    is_remote = args.server not in ("localhost", "127.0.0.1")
    api = APIClient(f"http://{args.server}:8000")
    check_prerequisites(api, args.video_path)
    client_id = ensure_task(args.task_id)
    push_url, pull_url = build_urls(args.server, client_id)

    ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")
    try:
        # 1. 推流
        stream_stabilize(ffmpeg, is_remote)

        # 2. start
        print(f"\n调用 /api/start (task_id={args.task_id}, pull_url={pull_url})")
        result = api.unified_start(args.task_id, pull_url, args.fps)
        if "error" in result:
            raise RuntimeError(f"/api/start 失败: {result['error']}")
        print(f"/api/start 成功: {result}")

        # 3. 运行 duration 秒
        if not args.no_window:
            viewer = InferenceViewer(client_id, show_window=True, base_port=f"{args.server}:8000")
            asyncio.run(viewer.connect_and_display(args.duration))
        else:
            print_viewer_url(args.server, client_id)
            print(f"运行中（无窗口，{args.duration}s）...")
            time.sleep(args.duration)

    finally:
        # 4. terminate
        print("\n调用 /api/terminate...")
        result = api.unified_terminate(client_id)
        print(f"terminate 结果: {result.get('status', result)}")
        ffmpeg.stop()

    print("\nScenario 1 完成")


# ---------------------------------------------------------------------------
# Scenario 2: 断流重连成功
# ---------------------------------------------------------------------------


def run_scenario_2(args):
    """
    断流后在重连窗口内恢复推流（gap < max_attempts × interval = 25s）。
    验证: 后端自动重连成功，无需重新调 /api/start。
    duration 建议 >= 50s（phase1≥15s + gap10s + phase2≥15s + 5s稳定）。
    """
    print("\n" + "=" * 60)
    print("Scenario 2: 断流重连成功")
    print(f"  phase1 = max(15, duration*0.35), gap = {RECONNECT_GAP}s")
    print("=" * 60)

    phase1 = max(15, int(args.duration * 0.35))
    is_remote = args.server not in ("localhost", "127.0.0.1")
    api = APIClient(f"http://{args.server}:8000")
    check_prerequisites(api, args.video_path)
    client_id = ensure_task(args.task_id)
    push_url, pull_url = build_urls(args.server, client_id)

    ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")
    try:
        # Phase 1: 推流并 start
        stream_stabilize(ffmpeg, is_remote)

        print(f"\n调用 /api/start (task_id={args.task_id})")
        result = api.unified_start(args.task_id, pull_url, args.fps)
        if "error" in result:
            raise RuntimeError(f"/api/start 失败: {result['error']}")
        print(f"/api/start 成功: {result}")

        print(f"\nPhase 1: 推流 {phase1}s...")
        time.sleep(phase1)

        # 断流
        print(f"\n断流（停止 FFmpeg）...")
        ffmpeg.stop()
        print(f"等待 {RECONNECT_GAP}s（后端将进入重连模式）...")
        time.sleep(RECONNECT_GAP)

        # Phase 2: 重新推流（后端自动重连，无需重调 start）
        print(f"\n重新推流（后端自动重连，无需重调 start）...")
        ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")
        stream_stabilize(ffmpeg, is_remote)

        remaining = max(args.duration - phase1 - RECONNECT_GAP, 10)
        print(f"\nPhase 2: 继续运行 {remaining}s...")

        if not args.no_window:
            viewer = InferenceViewer(client_id, show_window=True, base_port=f"{args.server}:8000")
            asyncio.run(viewer.connect_and_display(remaining))
        else:
            print_viewer_url(args.server, client_id)
            time.sleep(remaining)

    finally:
        print("\n调用 /api/terminate...")
        result = api.unified_terminate(client_id)
        print(f"terminate 结果: {result.get('status', result)}")
        ffmpeg.stop()

    print("\nScenario 2 完成")


# ---------------------------------------------------------------------------
# Scenario 3: 断流重连失败（等待后端自动清理）
# ---------------------------------------------------------------------------


def run_scenario_3(args):
    """
    断流后不再恢复，后端尝试 max_attempts 次重连失败后自动清理资源。
    不调用 /api/terminate，通过轮询 /health/status 确认自动清理。
    预期约 30s 后清理完成（heartbeat 5s + 5次重连×5s = 30s）。
    """
    print("\n" + "=" * 60)
    print("Scenario 3: 断流重连失败（自动清理）")
    print(f"  phase1 = max(15, duration*0.25)，然后永久断流")
    print(f"  预期 ~30s 后自动清理（不调 terminate）")
    print("=" * 60)

    phase1 = max(15, int(args.duration * 0.25))
    is_remote = args.server not in ("localhost", "127.0.0.1")
    api = APIClient(f"http://{args.server}:8000")
    check_prerequisites(api, args.video_path)
    client_id = ensure_task(args.task_id)
    push_url, pull_url = build_urls(args.server, client_id)

    ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")
    try:
        stream_stabilize(ffmpeg, is_remote)

        print(f"\n调用 /api/start (task_id={args.task_id})")
        result = api.unified_start(args.task_id, pull_url, args.fps)
        if "error" in result:
            raise RuntimeError(f"/api/start 失败: {result['error']}")
        print(f"/api/start 成功: {result}")

        print(f"\n推流 {phase1}s...")
        time.sleep(phase1)

        print(f"\n永久断流（停止 FFmpeg，不再恢复）...")
        ffmpeg.stop()

    except Exception:
        ffmpeg.stop()
        raise

    # 观察自动清理（不在 finally 中调 terminate）
    cleaned = poll_until_cleaned(api, client_id)
    if cleaned:
        print("\nPASS: 后端已自动清理资源")
    else:
        print("\nWARN: 自动清理未在预期时间内完成，请检查后端日志")

    print("\nScenario 3 完成")
    print("  请检查后端日志: RECONNECT MODE → 5次 RECONNECT ATTEMPT → RECONNECT FAILED → 清理")


# ---------------------------------------------------------------------------
# Scenario 4: 仅推流
# ---------------------------------------------------------------------------


def run_scenario_4(args):
    """
    仅推流到 MediaMTX，不调用任何后端 API。
    验证: MediaMTX 能正常接收流，后端不受未知流影响。
    """
    print("\n" + "=" * 60)
    print("Scenario 4: 仅推流（不调用后端 API）")
    print("=" * 60)

    is_remote = args.server not in ("localhost", "127.0.0.1")
    if not Path(args.video_path).exists():
        raise SystemExit(f"测试视频不存在: {args.video_path}")

    client_id = ensure_task(args.task_id)  # 仅用于生成 RTSP URL
    push_url, _ = build_urls(args.server, client_id)

    ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")
    try:
        stream_stabilize(ffmpeg, is_remote)
        print(f"\n推流中（{args.duration}s），后端未启动推理...")
        time.sleep(args.duration)
    finally:
        ffmpeg.stop()

    print("\nScenario 4 完成")


# ---------------------------------------------------------------------------
# Scenario 5: 仅 start
# ---------------------------------------------------------------------------


def run_scenario_5(args):
    mode = getattr(args, "mode", "no-stream")
    if mode == "no-terminate":
        _scenario5_no_terminate(args)
    else:
        _scenario5_no_stream(args)


def _scenario5_no_stream(args):
    """
    调用 /api/start 但不推流，模拟"忘记推流"的情况。
    后端会启动解码器但收不到帧，心跳超时后自动清理。
    不调 terminate，通过轮询确认自动清理。
    """
    print("\n" + "=" * 60)
    print("Scenario 5a: 仅 start，不推流（忘记推流）")
    print(f"  /api/start 后端通常会成功（FFmpeg进程启动但拉不到流）")
    print(f"  预期约 30s 后自动清理")
    print("=" * 60)

    api = APIClient(f"http://{args.server}:8000")
    check_prerequisites(api, args.video_path)
    client_id = ensure_task(args.task_id)
    _, pull_url = build_urls(args.server, client_id)

    print(f"\n调用 /api/start (无人推流, pull_url={pull_url})")
    result = api.unified_start(args.task_id, pull_url, args.fps)

    if "error" in result:
        print(f"\n/api/start 返回错误（也是有效的测试结果）: {result['error']}")
        print("后端在流不可达时拒绝了请求")
    else:
        print(f"/api/start 成功: {result}")
        print("\n（无流推送，等待心跳超时触发自动清理）")
        cleaned = poll_until_cleaned(api, client_id)
        if cleaned:
            print("\nPASS: 后端已自动清理孤儿 session")
        else:
            print("\nWARN: 未确认自动清理，手动 terminate...")
            api.unified_terminate(client_id)

    print("\nScenario 5a 完成")


def _scenario5_no_terminate(args):
    """
    正常推流和 start，但测试结束时不调 terminate，模拟"忘记停止"。
    FFmpeg 停止后后端应能通过孤儿检测自动清理。
    """
    print("\n" + "=" * 60)
    print("Scenario 5b: 推流 + start，但不 terminate（忘记停止）")
    print(f"  测试结束后检查后端日志确认孤儿检测")
    print("=" * 60)

    is_remote = args.server not in ("localhost", "127.0.0.1")
    api = APIClient(f"http://{args.server}:8000")
    check_prerequisites(api, args.video_path)
    client_id = ensure_task(args.task_id)
    push_url, pull_url = build_urls(args.server, client_id)

    ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")
    try:
        stream_stabilize(ffmpeg, is_remote)

        print(f"\n调用 /api/start (task_id={args.task_id})")
        result = api.unified_start(args.task_id, pull_url, args.fps)
        if "error" in result:
            raise RuntimeError(f"/api/start 失败: {result['error']}")
        print(f"/api/start 成功: {result}")

        print(f"\n运行 {args.duration}s，结束后不调 terminate...")
        time.sleep(args.duration)
        # 不调 /api/terminate
    finally:
        ffmpeg.stop()  # FFmpeg 停止，后端将进入重连/孤儿检测

    print("\nScenario 5b 完成")
    print("  请检查后端日志确认孤儿检测和自动清理流程")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="CleanSight 单客户端集成测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
场景说明:
  1  正常流程:        推流 → start → 等待 duration → terminate
  2  断流重连(成功):  推流 → start → 断流10s → 重连 → terminate  (duration >= 50s)
  3  断流重连(失败):  推流 → start → 永久断流 → 等待自动清理
  4  仅推流:          推流 duration 秒，不调任何 API
  5  仅 start:        --mode no-stream: start 但不推流
                       --mode no-terminate: start 但不 terminate
        """,
    )
    parser.add_argument("--scenario", type=int, required=True, choices=[1, 2, 3, 4, 5], help="测试场景编号")
    parser.add_argument("--server", default="localhost", help="服务器地址（默认: localhost）")
    parser.add_argument("--task_id", type=int, required=True, help="任务 ID")
    parser.add_argument("--duration", type=int, default=60, help="运行时长（秒，默认: 60）")
    parser.add_argument("--video_path", default=None, help="测试视频路径（默认: test/test_video.mp4）")
    parser.add_argument("--fps", type=int, default=30, help="推流帧率（默认: 30）")
    parser.add_argument("--no-window", action="store_true", dest="no_window", help="禁用 OpenCV 可视化窗口")
    parser.add_argument(
        "--mode",
        choices=["no-stream", "no-terminate"],
        default="no-stream",
        help="Scenario 5 子模式（默认: no-stream）",
    )
    args = parser.parse_args()

    if args.video_path is None:
        args.video_path = str(Path(__file__).parent.parent / "test" / "test_video.mp4")

    dispatch = {
        1: run_scenario_1,
        2: run_scenario_2,
        3: run_scenario_3,
        4: run_scenario_4,
        5: run_scenario_5,
    }

    try:
        dispatch[args.scenario](args)
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
