"""
单客户端集成测试 - 覆盖6种使用场景

用法:
    python integration_tests/test_single_client.py --scenario <1-7> --task_id <id> [options]

场景:
    1 - 正常流程:   推流 → start → 等待 → terminate
    2 - 断流重连(成功): 推流 → start → 断流 → 重连 → terminate
    3 - 断流重连(失败): 推流 → start → 断流 → 等待自动清理
    4 - 仅推流:     推流，不调用任何 API
    5 - 仅 start:   调 start 但不推流(no-stream) 或 不调 terminate(no-terminate)
    6 - 延迟推流:   先调 start（流未就绪），N秒后推流，验证健康监控自动重连（Bug 2）
    7 - MOCK阶段:   无效 current_step → MOCK fallback，验证帧透传不黑屏

参数:
    --scenario    {1,2,3,4,5,6}       必填
    --server      <host>               默认 localhost
    --task_id     <int>                必填
    --duration    <seconds>            默认 60
    --video_path  <path>               默认 test/test_video.mp4
    --fps         <int>                默认 30
    --no-window                        禁用 OpenCV 可视化窗口
    --mode        no-stream|no-terminate  仅 scenario 5，默认 no-stream
    --stream-delay <seconds>           仅 scenario 6，推流延迟（默认 10s）
"""

import argparse
import asyncio
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.client_viewer import InferenceViewer
from integration_tests.utils import APIClient, DatabaseHelper, FFmpegController

# 自动清理超时: heartbeat(5s) + max_attempts(5) × interval(5s) + buffer(10s) = 40s
AUTO_CLEANUP_TIMEOUT = 45
# 断流重连成功场景: gap 必须 < max_attempts×interval = 25s
RECONNECT_GAP = 10
# Scenario 6: 推流延迟默认值（秒）—— 必须 < 重连窗口 25s
DELAYED_STREAM_DEFAULT = 10
# Scenario 6: 等待重连成功的最大轮询时长（秒）
RECONNECT_SUCCESS_TIMEOUT = 45


# ---------------------------------------------------------------------------
# 共享帮助函数
# ---------------------------------------------------------------------------


def build_urls(server: str, client_id: str) -> tuple:
    """
    返回 (push_url, pull_url)。
    push_url == pull_url：推流目标即后端拉流地址，后端内部会 rewrite 为 127.0.0.1。
    """
    # Windows 上 localhost 可能解析为 ::1（IPv6），但 RTSPProxy 只监听 IPv4。
    # 本地推流强制用 127.0.0.1；远程推流保留原始 server 地址。
    push_host = "127.0.0.1" if server in ("localhost", "127.0.0.1") else server
    push_url = f"rtsp://{push_host}:8004/live/{client_id}"
    return push_url, push_url


@contextmanager
def managed_task(task_id: int, current_step: str = "1"):
    """确保任务存在于数据库，退出时自动清理自己创建的任务。

    Yields:
        client_id (str): 即 task.source_ip
    """
    db = DatabaseHelper()
    task = db.get_task(task_id)
    created = False
    if not task:
        source_ip = f"test.s{task_id}"
        print(f"任务 {task_id} 不存在，创建新任务 (source_ip={source_ip}, current_step={current_step})")
        db.create_test_task(task_id, source_ip=source_ip, current_step=current_step)
        task = db.get_task(task_id)
        created = True
    client_id = str(task.source_ip)
    print(f"client_id: {client_id}")
    try:
        yield client_id
    finally:
        if created:
            print(f"\n清理测试创建的任务 {task_id}...")
            db.cleanup_test_task(task_id)


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


def poll_until_reconnected(api: APIClient, client_id: str, timeout: int = RECONNECT_SUCCESS_TIMEOUT) -> bool:
    """
    轮询 GET /health/status，等待 client_id 重连成功：
      - 出现在 queues 中（ClientQueues 存在）
      - 不再出现在 reconnecting_clients 中（连接已稳定）

    分两个阶段打印状态，便于观察状态机转换：
      阶段一：等待进入重连模式（reconnecting_clients 出现 client_id）
      阶段二：等待退出重连模式（连接稳定）

    返回 True 表示重连成功，False 表示超时。
    """
    deadline = time.time() + timeout
    print(f"  等待重连成功（最多 {timeout}s）：client_id={client_id}")
    entered_reconnect = False

    while time.time() < deadline:
        status = api._make_request("GET", "/health/status")
        queues = status.get("queues", {})
        reconnecting = status.get("monitor_stats", {}).get("reconnecting_clients", [])
        remaining = int(deadline - time.time())

        in_queues = client_id in queues
        in_reconnecting = client_id in reconnecting

        if in_reconnecting and not entered_reconnect:
            entered_reconnect = True
            print(f"  [{remaining}s 剩余] 已进入重连模式（符合预期，等待流就绪）")
        elif in_queues and not in_reconnecting:
            label = "重连成功" if entered_reconnect else "连接建立（流已就绪，未经过重连模式）"
            print(f"  [{remaining}s 剩余] {label}")
            return True
        else:
            mode = "重连中" if in_reconnecting else ("已在队列" if in_queues else "未见于健康状态")
            print(f"  [{remaining}s 剩余] {mode}，等待稳定...")

        time.sleep(2)

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

    with managed_task(args.task_id) as client_id:
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

    with managed_task(args.task_id) as client_id:
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

    with managed_task(args.task_id) as client_id:
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

    with managed_task(args.task_id) as client_id:
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

    with managed_task(args.task_id) as client_id:
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

    with managed_task(args.task_id) as client_id:
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
# Scenario 6: 延迟推流 — Bug 2 重连验证
# ---------------------------------------------------------------------------


def run_scenario_6(args):
    """
    推流晚于拉流启动：先调 /api/start（无流），N 秒后推流，验证健康监控自动重连。

    复现场景：
      1. 调 /api/start → FFmpeg 拉流失败（流未就绪）→ API 返回错误
      2. 等待 --stream-delay 秒（默认 8s）后启动 FFmpeg 推流
      3. 健康监控检测到 dead decoder → 进入重连模式 → 触发 restart_stream
      4. 重连成功后运行片刻，最后调 terminate

    Bug 2 验证点：
      - 修复前：decoder 未注册 → 健康监控走 orphan 路径 → 永不重连
      - 修复后：decoder 先注册 → idle_time > 5s → 进入重连模式 → 自动建立连接

    后端日志关键字：
      'Initial start failed, health monitor will retry'
      'RECONNECT MODE' → 'restart_stream' → 'Stream restarted successfully'

    参数说明：
      --stream-delay  推流延迟秒数，必须 < 25s（重连窗口 = max_attempts × interval）
      --duration      重连成功后的稳定观察时长
    """
    stream_delay = getattr(args, "stream_delay", DELAYED_STREAM_DEFAULT)

    print("\n" + "=" * 60)
    print("Scenario 6: 延迟推流 — 初始拉流失败后健康监控自动重连")
    print(f"  推流延迟: {stream_delay}s（须 < 重连窗口 25s）")
    print(f"  重连成功超时: {RECONNECT_SUCCESS_TIMEOUT}s")
    print("=" * 60)

    is_remote = args.server not in ("localhost", "127.0.0.1")
    api = APIClient(f"http://{args.server}:8000")
    check_prerequisites(api, args.video_path)

    with managed_task(args.task_id) as client_id:
        push_url, pull_url = build_urls(args.server, client_id)
        ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")

        try:
            # ----------------------------------------------------------------
            # Step 1: 先调 /api/start（此时无流，预期失败或成功均可）
            # ----------------------------------------------------------------
            print(f"\n[Step 1] 调用 /api/start（流尚未就绪）")
            print(f"  pull_url={pull_url}")
            result = api.unified_start(args.task_id, pull_url, args.fps)

            if "error" in result:
                print(f"  [预期] /api/start 返回错误（流不可达）")
                print(f"  错误摘要: {str(result.get('error', ''))[:100]}")
                print(f"  → 修复验证：decoder 已注册，健康监控将在约 5s 后进入重连模式")
            else:
                print(f"  /api/start 意外成功（流在调用瞬间已就绪），测试仍继续")
                print(f"  响应: {result}")

            # ----------------------------------------------------------------
            # Step 2: 等待推流延迟（模拟推流端稍后启动）
            # ----------------------------------------------------------------
            print(f"\n[Step 2] 等待 {stream_delay}s（模拟推流端准备中）...")
            time.sleep(stream_delay)

            # ----------------------------------------------------------------
            # Step 3: 启动推流
            # ----------------------------------------------------------------
            print(f"\n[Step 3] 启动推流: {push_url}")
            stream_stabilize(ffmpeg, is_remote)
            print(f"  推流已就绪，健康监控将检测到流并触发 restart_stream")

            # ----------------------------------------------------------------
            # Step 4: 等待健康监控重连成功
            # ----------------------------------------------------------------
            print(f"\n[Step 4] 轮询健康状态，等待重连成功...")
            reconnected = poll_until_reconnected(api, client_id, timeout=RECONNECT_SUCCESS_TIMEOUT)

            if reconnected:
                print(f"\n[PASS] 健康监控自动重连成功（无需再次调 /api/start）")

                # 稳定运行片刻，确认连接质量
                stable_secs = min(args.duration, 15)
                print(f"\n[Step 5] 稳定观察 {stable_secs}s...")
                if not args.no_window:
                    viewer = InferenceViewer(
                        client_id, show_window=True, base_port=f"{args.server}:8000"
                    )
                    asyncio.run(viewer.connect_and_display(stable_secs))
                else:
                    print_viewer_url(args.server, client_id)
                    time.sleep(stable_secs)
            else:
                print(f"\n[FAIL] {RECONNECT_SUCCESS_TIMEOUT}s 内未检测到重连成功")
                print(f"  排查步骤:")
                print(f"  1. 检查后端日志是否有 'Initial start failed, health monitor will retry'")
                print(f"     → 有: 修复生效，可能重连窗口不够（增大 --stream-delay 或减小延迟）")
                print(f"     → 无: Bug 2 修复未生效，请重新检查 service.py 注册顺序")
                print(f"  2. 检查后端日志是否有 'orphan' 相关日志")
                print(f"     → 有: decoder 未注册，健康监控走了 orphan 路径（旧 bug 行为）")

        finally:
            print(f"\n[Cleanup] 调用 /api/terminate...")
            term_result = api.unified_terminate(client_id)
            print(f"terminate 结果: {term_result.get('status', term_result)}")
            ffmpeg.stop()

    print("\nScenario 6 完成")
    print("  后端日志验证路径:")
    print("  'Initial start failed' → 'RECONNECT MODE' → 'restart_stream' → 'Stream restarted successfully'")


# ---------------------------------------------------------------------------
# Scenario 7: 无效 current_step → MOCK 阶段透传
# ---------------------------------------------------------------------------


def run_scenario_7(args):
    """
    使用无效 current_step 启动任务，验证 MOCK 阶段 fallback 不黑屏。

    验证点：
      - current_step="未知阶段" 路由到 MOCK stage
      - WebSocket 能正常收到视频帧（不黑屏）
      - terminate 正常清理资源

    后端日志关键字：
      'Routing client ... to stage MOCK'
      InferWorker-MOCK 线程正常运行
    """
    print("\n" + "=" * 60)
    print("Scenario 7: 无效 current_step → MOCK 阶段透传（验证不黑屏）")
    print(f"  current_step = '未知阶段' → 预期路由到 MOCK stage")
    print("=" * 60)

    is_remote = args.server not in ("localhost", "127.0.0.1")
    api = APIClient(f"http://{args.server}:8000")
    check_prerequisites(api, args.video_path)

    with managed_task(args.task_id, current_step="未知阶段") as client_id:
        push_url, pull_url = build_urls(args.server, client_id)

        ffmpeg = FFmpegController(args.video_path, push_url, protocol="rtsp")
        try:
            stream_stabilize(ffmpeg, is_remote)

            print(f"\n调用 /api/start (task_id={args.task_id}, current_step=未知阶段)")
            result = api.unified_start(args.task_id, pull_url, args.fps)
            if "error" in result:
                raise RuntimeError(f"/api/start 失败: {result['error']}")
            print(f"/api/start 成功: {result}")
            print(f"  → 检查后端日志确认已路由到 MOCK stage")

            if not args.no_window:
                viewer = InferenceViewer(client_id, show_window=True, base_port=f"{args.server}:8000")
                asyncio.run(viewer.connect_and_display(args.duration))
            else:
                print_viewer_url(args.server, client_id)
                print(f"运行中（无窗口，{args.duration}s）...")
                time.sleep(args.duration)

        finally:
            print("\n调用 /api/terminate...")
            result = api.unified_terminate(client_id)
            print(f"terminate 结果: {result.get('status', result)}")
            ffmpeg.stop()

    print("\nScenario 7 完成")
    print("  验证: 后端日志应有 MOCK stage 路由，WebSocket 帧正常推送（无黑屏）")


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
  6  延迟推流:        先 start（无流，预期失败）→ N秒后推流 → 验证自动重连 (Bug 2)
  7  MOCK阶段:        无效 current_step → MOCK fallback → 验证帧透传不黑屏
        """,
    )
    parser.add_argument("--scenario", type=int, required=True, choices=[1, 2, 3, 4, 5, 6, 7], help="测试场景编号")
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
    parser.add_argument(
        "--stream-delay",
        type=int,
        default=DELAYED_STREAM_DEFAULT,
        dest="stream_delay",
        help=f"Scenario 6：推流延迟秒数，须 < 25s（默认: {DELAYED_STREAM_DEFAULT}s）",
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
        6: run_scenario_6,
        7: run_scenario_7,
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
