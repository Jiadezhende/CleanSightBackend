"""
单客户端集成测试 - 覆盖 9 种使用场景（观测走 admin 运维面板 /admin-f3m8/ui/）

用法:
    python integration_tests/test_single_client.py --scenario <1-9> --task_id <id> [options]

场景:
    1 - 正常流程:   推流 → start → 等待 → terminate
    2 - 断流重连(成功): 推流 → start → 断流 → 重连 → terminate
    3 - 断流重连(失败): 推流 → start → 断流 → 等待自动清理
    4 - 仅推流:     推流，不调用任何 API
    5 - 仅 start:   调 start 但不推流(no-stream) 或 不调 terminate(no-terminate)
    6 - 延迟推流:   先调 start（流未就绪），N秒后推流，验证健康监控自动重连（Bug 2）
    7 - CLEAN阶段:  current_step=2 → CLEAN stage，验证帧透传不黑屏
    8 - MOCK阶段:   无效 current_step → MOCK fallback，验证帧透传不黑屏
    9 - 阶段切换:   start(LEAK) → DB 改 step=2 → 再 start 触发全量重建 → CLEAN

参数:
    --scenario    {1-9}                必填
    --server      <host>               默认 localhost
    --api-port    <int>                默认 8000（后端 HTTP/WS API 端口）
    --rtsp-port   <int>                默认 8004（RTSPProxy 推流端口）
    --task_id     <int>                必填
    --duration    <seconds>            默认 60
    --video_path  <path>               默认 test/test_video.mp4
    --fps         <int>                默认 30
    --mode        no-stream|no-terminate  仅 scenario 5，默认 no-stream
    --stream-delay <seconds>           仅 scenario 6，推流延迟（默认 10s）
    --current-step <step>              任务阶段(1=LEAK/2=CLEAN/其它=MOCK)，覆盖场景默认

维度说明:
    --scenario     决定「怎么跑」（生命周期：正常/断流/延迟/不 terminate…）
    --current-step 决定「跑什么」（任务阶段 → 推理 workflow）
    两者正交，可自由组合，例如 --scenario 2 --current-step 2 = CLEAN 阶段断流重连。
"""

import argparse
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.utils import APIClient, DatabaseHelper, FFmpegController

# 自动清理超时: 重连判据为「进程死活」，放弃(cleanup)纯按无帧时长触发——
# 无帧 ≥ cleanup_timeout(config/health_monitor_config.yaml，直配 20s) + buffer
AUTO_CLEANUP_TIMEOUT = 35
# 断流重连成功场景: gap 必须 < cleanup_timeout(20s)，否则恢复前就被清理
RECONNECT_GAP = 10
# Scenario 6: 推流延迟默认值（秒）—— 必须 < cleanup_timeout(20s)
DELAYED_STREAM_DEFAULT = 10
# Scenario 6: 等待重连成功的最大轮询时长（秒）
RECONNECT_SUCCESS_TIMEOUT = 45

# 服务端口默认值（唯一来源，仅作 argparse 默认；运行期端口随 args 传递）。
# 对应标准部署；测试环境可能做了端口偏移（如 8100/8104），用 --api-port / --rtsp-port 指定。
DEFAULT_API_PORT = 8000   # 后端 HTTP/WS API 端口
DEFAULT_RTSP_PORT = 8004  # RTSPProxy 对外推流端口


# ---------------------------------------------------------------------------
# 共享帮助函数
# ---------------------------------------------------------------------------


def is_local(server: str) -> bool:
    """server 是否为本机地址（决定推流 host 与推流稳定等待时长）。"""
    return server in ("localhost", "127.0.0.1")


def build_urls(server: str, client_id: str, rtsp_port: int) -> tuple:
    """
    返回 (push_url, pull_url)。
    push_url == pull_url：推流目标即后端拉流地址，后端内部会 rewrite 为 127.0.0.1。
    """
    # Windows 上 localhost 可能解析为 ::1（IPv6），但 RTSPProxy 只监听 IPv4。
    # 本地推流强制用 127.0.0.1；远程推流保留原始 server 地址。
    push_host = "127.0.0.1" if is_local(server) else server
    push_url = f"rtsp://{push_host}:{rtsp_port}/live/{client_id}"
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
    else:
        # 复用已存在任务时，DB 里的 current_step 才是后端实际路由依据。
        # 若与本次请求不一致，过去会被静默忽略 → 「以为测 CLEAN，实际跑 LEAK」。
        # 这里 fail-fast，不擅自改写可能是真实任务的 current_step。
        existing_step = str(task.current_step)
        if existing_step != current_step:
            raise SystemExit(
                f"任务 {task_id} 已存在且 current_step={existing_step!r}，与本次请求的 "
                f"{current_step!r} 不一致。\n"
                f"为避免测错阶段，请二选一：\n"
                f"  1) 换一个未占用的 --task_id（测试会自建并在结束时自动清理）；\n"
                f"  2) 加 --current-step {existing_step} 显式复用现有任务的阶段。"
            )
        print(f"复用已存在任务 {task_id} (current_step={existing_step})")
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


@contextmanager
def scenario_setup(args, *, current_step="1", need_stream=True, check_health=True):
    """统一的 scenario 初始化脚手架。

    依次完成：构造 APIClient → 前置检查 → 准备任务（managed_task）→ 构造推流 URL
    与 FFmpegController，并以 SimpleNamespace 暴露给 scenario 使用。

    Args:
        current_step: 场景默认阶段值；命令行 --current-step 若指定则优先覆盖。
        need_stream:  是否需要 FFmpegController（仅 start 不推流的场景设为 False）。
        check_health: True 走完整前置检查；False 仅校验视频存在（不依赖后端）。

    Yields:
        SimpleNamespace(api, client_id, push_url, pull_url, ffmpeg, is_remote, current_step)
    """
    # --current-step 覆盖场景默认值（决定路由到哪个推理 workflow）
    step = args.current_step if args.current_step is not None else current_step

    api = APIClient(f"http://{args.server}:{args.api_port}")
    if check_health:
        check_prerequisites(api, args.video_path)
    elif not Path(args.video_path).exists():
        raise SystemExit(f"测试视频不存在: {args.video_path}")

    with managed_task(args.task_id, step) as client_id:
        push_url, pull_url = build_urls(args.server, client_id, args.rtsp_port)
        ffmpeg = (
            FFmpegController(args.video_path, push_url, protocol="rtsp")
            if need_stream
            else None
        )
        yield SimpleNamespace(
            api=api,
            client_id=client_id,
            push_url=push_url,
            pull_url=pull_url,
            ffmpeg=ffmpeg,
            is_remote=not is_local(args.server),
            current_step=step,
        )


def section(title: str, *extra: str):
    """打印场景分隔标题（含可选的缩进说明行）。"""
    print("\n" + "=" * 60)
    print(title)
    for line in extra:
        print(f"  {line}")
    print("=" * 60)


def stream_stabilize(ffmpeg: FFmpegController, is_remote: bool):
    """启动 FFmpeg 推流并等待稳定。"""
    if not ffmpeg.start():
        raise RuntimeError("FFmpeg 推流启动失败")
    wait = 5 if is_remote else 3
    print(f"等待推流稳定 ({wait}s)...")
    time.sleep(wait)


def do_start(api: APIClient, args, pull_url: str, *, label: str = ""):
    """调用 /api/start，失败时抛 RuntimeError，成功时打印响应并返回结果。"""
    print(f"\n调用 /api/start ({label or f'task_id={args.task_id}'})")
    result = api.unified_start(args.task_id, pull_url, args.fps)
    if "error" in result:
        raise RuntimeError(f"/api/start 失败: {result['error']}")
    print(f"/api/start 成功: {result}")
    return result


def watch_or_sleep(args, client_id: str, duration: int):
    """运行 duration 秒；观测走 admin 运维面板（打印一次面板 URL）。"""
    print_admin_url(args.server, args.api_port, args.task_id)
    print(f"运行中（{duration}s）...")
    time.sleep(duration)


def do_terminate(api: APIClient, client_id: str, ffmpeg: FFmpegController = None):
    """调用 /api/terminate，并在提供 ffmpeg 时一并停止推流。"""
    print("\n调用 /api/terminate...")
    result = api.unified_terminate(client_id)
    print(f"terminate 结果: {result.get('status', result)}")
    if ffmpeg:
        ffmpeg.stop()


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


_admin_url_printed = False


def print_admin_url(server: str, api_port: int, task_id: int):
    """打印 admin 运维面板 URL（观测唯一入口），每次运行只打印一次。"""
    global _admin_url_printed
    if _admin_url_printed:
        return
    _admin_url_printed = True
    print(f"\n观测走 admin 运维面板（后端自带，同源同端口）:")
    print(f"  http://{server}:{api_port}/admin-f3m8/ui/")
    print(f"  → 「实时监控」tab 选择 task_id={task_id} 对应的客户端并点「连接」")
    print(f"  → 告警/指标/证据回溯见其余 tab\n")


# ---------------------------------------------------------------------------
# 标准生命周期：推流 → start → 观察 duration → terminate
# Scenario 1/7/8 共用，仅 current_step 与提示文案不同
# ---------------------------------------------------------------------------


def _run_simple_lifecycle(args, *, name, subtitle, current_step_default, extra=(), tail=()):
    """推流 → start → 观察 duration → terminate 的标准生命周期。

    Args:
        name:                 场景名（如 "Scenario 1"），用于标题与完成提示。
        subtitle:             标题副标题。
        current_step_default: 场景默认 current_step；--current-step 可覆盖。
        extra:                标题下的缩进说明行。
        tail:                 完成后追加打印的提示行（如日志关键字）。
    """
    section(f"{name}: {subtitle}", *extra)

    with scenario_setup(args, current_step=current_step_default) as ctx:
        try:
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)
            do_start(ctx.api, args, ctx.pull_url,
                     label=f"task_id={args.task_id}, current_step={ctx.current_step}")
            watch_or_sleep(args, ctx.client_id, args.duration)
        finally:
            do_terminate(ctx.api, ctx.client_id, ctx.ffmpeg)

    print(f"\n{name} 完成")
    for line in tail:
        print(line)


# ---------------------------------------------------------------------------
# Scenario 1: 正常流程
# ---------------------------------------------------------------------------


def run_scenario_1(args):
    """正常使用流程：推流 → start → 等待 duration → terminate。

    验证: 完整的推理可视化、后端正常响应、terminate 成功清理资源。
    current_step 默认 1（LEAK），可用 --current-step 指定任意阶段。
    """
    _run_simple_lifecycle(
        args,
        name="Scenario 1",
        subtitle="正常流程",
        current_step_default="1",
    )


# ---------------------------------------------------------------------------
# Scenario 2: 断流重连成功
# ---------------------------------------------------------------------------


def run_scenario_2(args):
    """
    断流后在重连窗口内恢复推流（gap < max_attempts × interval = 25s）。
    验证: 后端自动重连成功，无需重新调 /api/start。
    duration 建议 >= 50s（phase1≥15s + gap10s + phase2≥15s + 5s稳定）。
    """
    phase1 = max(15, int(args.duration * 0.35))
    section("Scenario 2: 断流重连成功",
            f"phase1 = max(15, duration*0.35), gap = {RECONNECT_GAP}s")

    with scenario_setup(args) as ctx:
        try:
            # Phase 1: 推流并 start
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)
            do_start(ctx.api, args, ctx.pull_url)

            print(f"\nPhase 1: 推流 {phase1}s...")
            time.sleep(phase1)

            # 断流
            print(f"\n断流（停止 FFmpeg）...")
            ctx.ffmpeg.stop()
            print(f"等待 {RECONNECT_GAP}s（后端将进入重连模式）...")
            time.sleep(RECONNECT_GAP)

            # Phase 2: 重新推流（后端自动重连，无需重调 start）
            print(f"\n重新推流（后端自动重连，无需重调 start）...")
            ctx.ffmpeg = FFmpegController(args.video_path, ctx.push_url, protocol="rtsp")
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)

            remaining = max(args.duration - phase1 - RECONNECT_GAP, 10)
            print(f"\nPhase 2: 继续运行 {remaining}s...")
            watch_or_sleep(args, ctx.client_id, remaining)
        finally:
            do_terminate(ctx.api, ctx.client_id, ctx.ffmpeg)

    print("\nScenario 2 完成")


# ---------------------------------------------------------------------------
# Scenario 3: 断流重连失败（等待后端自动清理）
# ---------------------------------------------------------------------------


def run_scenario_3(args):
    """
    断流后不再恢复：后端 decoder 进程退出 → 反复 respawn 均连不上 → 无帧超 cleanup_timeout
    后自动清理资源。不调用 /api/terminate，通过轮询 /health/status 确认自动清理。
    预期约 20s 后清理完成（无帧 ≥ cleanup_timeout=20s，纯时间触发、不数次数）。
    """
    phase1 = max(15, int(args.duration * 0.25))
    section("Scenario 3: 断流重连失败（自动清理）",
            "phase1 = max(15, duration*0.25)，然后永久断流",
            "预期 ~20s 后自动清理（不调 terminate）")

    with scenario_setup(args) as ctx:
        try:
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)
            do_start(ctx.api, args, ctx.pull_url)

            print(f"\n推流 {phase1}s...")
            time.sleep(phase1)

            print(f"\n永久断流（停止 FFmpeg，不再恢复）...")
            ctx.ffmpeg.stop()
        except Exception:
            ctx.ffmpeg.stop()
            raise

        # 观察自动清理（不在 finally 中调 terminate）
        cleaned = poll_until_cleaned(ctx.api, ctx.client_id)
        if cleaned:
            print("\nPASS: 后端已自动清理资源")
        else:
            print("\nWARN: 自动清理未在预期时间内完成，请检查后端日志")

    print("\nScenario 3 完成")
    print("  请检查后端日志: RECONNECT MODE → RECONNECT ATTEMPT(多次,~每5s) → RECONNECT FAILED → 清理")


# ---------------------------------------------------------------------------
# Scenario 4: 仅推流
# ---------------------------------------------------------------------------


def run_scenario_4(args):
    """
    仅推流到 MediaMTX，不调用任何后端 API。
    验证: MediaMTX 能正常接收流，后端不受未知流影响。
    """
    section("Scenario 4: 仅推流（不调用后端 API）")

    with scenario_setup(args, check_health=False) as ctx:
        try:
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)
            print(f"\n推流中（{args.duration}s），后端未启动推理...")
            time.sleep(args.duration)
        finally:
            ctx.ffmpeg.stop()

    print("\nScenario 4 完成")


# ---------------------------------------------------------------------------
# Scenario 5: 仅 start
# ---------------------------------------------------------------------------


def run_scenario_5(args):
    if args.mode == "no-terminate":
        _scenario5_no_terminate(args)
    else:
        _scenario5_no_stream(args)


def _scenario5_no_stream(args):
    """
    调用 /api/start 但不推流，模拟"忘记推流"的情况。
    后端会启动解码器但收不到帧，心跳超时后自动清理。
    不调 terminate，通过轮询确认自动清理。
    """
    section("Scenario 5a: 仅 start，不推流（忘记推流）",
            "/api/start 后端通常会成功（FFmpeg进程启动但拉不到流）",
            "预期约 30s 后自动清理")

    with scenario_setup(args, need_stream=False) as ctx:
        print(f"\n调用 /api/start (无人推流, pull_url={ctx.pull_url})")
        result = ctx.api.unified_start(args.task_id, ctx.pull_url, args.fps)

        if "error" in result:
            print(f"\n/api/start 返回错误（也是有效的测试结果）: {result['error']}")
            print("后端在流不可达时拒绝了请求")
        else:
            print(f"/api/start 成功: {result}")
            print("\n（无流推送，等待心跳超时触发自动清理）")
            cleaned = poll_until_cleaned(ctx.api, ctx.client_id)
            if cleaned:
                print("\nPASS: 后端已自动清理孤儿 session")
            else:
                print("\nWARN: 未确认自动清理，手动 terminate...")
                ctx.api.unified_terminate(ctx.client_id)

    print("\nScenario 5a 完成")


def _scenario5_no_terminate(args):
    """
    正常推流和 start，但测试结束时不调 terminate，模拟"忘记停止"。
    FFmpeg 停止后后端应能通过孤儿检测自动清理。
    """
    section("Scenario 5b: 推流 + start，但不 terminate（忘记停止）",
            "测试结束后检查后端日志确认孤儿检测")

    with scenario_setup(args) as ctx:
        try:
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)
            do_start(ctx.api, args, ctx.pull_url)

            print(f"\n运行 {args.duration}s，结束后不调 terminate...")
            time.sleep(args.duration)
            # 不调 /api/terminate
        finally:
            ctx.ffmpeg.stop()  # FFmpeg 停止，后端将进入重连/孤儿检测

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
      - 修复后：decoder 先注册但 start() 失败 → is_decoder_alive=False → 下一 tick 进入重连
        → 反复 respawn，直到推流就绪后某次 respawn 连上 → 来帧退出重连

    后端日志关键字：
      'Initial start failed, health monitor will retry'
      'RECONNECT MODE' → 'restart_stream' → 'Stream restarted successfully'

    参数说明：
      --stream-delay  推流延迟秒数，必须 < cleanup_timeout(20s)（否则来帧前已被清理）
      --duration      重连成功后的稳定观察时长
    """
    stream_delay = args.stream_delay
    section("Scenario 6: 延迟推流 — 初始拉流失败后健康监控自动重连",
            f"推流延迟: {stream_delay}s（须 < cleanup_timeout 20s）",
            f"重连成功超时: {RECONNECT_SUCCESS_TIMEOUT}s")

    with scenario_setup(args) as ctx:
        try:
            # ----------------------------------------------------------------
            # Step 1: 先调 /api/start（此时无流，预期失败或成功均可）
            # ----------------------------------------------------------------
            print(f"\n[Step 1] 调用 /api/start（流尚未就绪）")
            print(f"  pull_url={ctx.pull_url}")
            result = ctx.api.unified_start(args.task_id, ctx.pull_url, args.fps)

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
            print(f"\n[Step 3] 启动推流: {ctx.push_url}")
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)
            print(f"  推流已就绪，健康监控将检测到流并触发 restart_stream")

            # ----------------------------------------------------------------
            # Step 4: 等待健康监控重连成功
            # ----------------------------------------------------------------
            print(f"\n[Step 4] 轮询健康状态，等待重连成功...")
            reconnected = poll_until_reconnected(ctx.api, ctx.client_id, timeout=RECONNECT_SUCCESS_TIMEOUT)

            if reconnected:
                print(f"\n[PASS] 健康监控自动重连成功（无需再次调 /api/start）")

                # 稳定运行片刻，确认连接质量
                stable_secs = min(args.duration, 15)
                print(f"\n[Step 5] 稳定观察 {stable_secs}s...")
                watch_or_sleep(args, ctx.client_id, stable_secs)
            else:
                print(f"\n[FAIL] {RECONNECT_SUCCESS_TIMEOUT}s 内未检测到重连成功")
                print(f"  排查步骤:")
                print(f"  1. 检查后端日志是否有 'Initial start failed, health monitor will retry'")
                print(f"     → 有: 修复生效，可能重连窗口不够（增大 --stream-delay 或减小延迟）")
                print(f"     → 无: Bug 2 修复未生效，请重新检查 service.py 注册顺序")
                print(f"  2. 检查后端日志是否有 'orphan' 相关日志")
                print(f"     → 有: decoder 未注册，健康监控走了 orphan 路径（旧 bug 行为）")

        finally:
            print(f"\n[Cleanup]", end=" ")
            do_terminate(ctx.api, ctx.client_id, ctx.ffmpeg)

    print("\nScenario 6 完成")
    print("  后端日志验证路径:")
    print("  'Initial start failed' → 'RECONNECT MODE' → 'restart_stream' → 'Stream restarted successfully'")


# ---------------------------------------------------------------------------
# Scenario 7: current_step="2" → CLEAN 阶段透传（复现生产黑屏问题）
# ---------------------------------------------------------------------------


def run_scenario_7(args):
    """current_step=2 → CLEAN 阶段透传（验证不黑屏）。

    本质是「标准生命周期 + current_step=2」的预设别名，等价于
    `--scenario 1 --current-step 2`。--current-step 可进一步覆盖。
    """
    _run_simple_lifecycle(
        args,
        name="Scenario 7",
        subtitle="current_step=2 → CLEAN 阶段透传（验证不黑屏）",
        current_step_default="2",
        extra=("current_step = '2' → 预期路由到 CLEAN stage",),
        tail=("  验证: InferWorker-CLEAN 正常运行，WebSocket 帧正常推送（无黑屏）",),
    )


# ---------------------------------------------------------------------------
# Scenario 8: 无效 current_step → MOCK 阶段透传
# ---------------------------------------------------------------------------


def run_scenario_8(args):
    """无效 current_step → MOCK 阶段 fallback（验证不黑屏）。

    本质是「标准生命周期 + 无效 current_step」的预设别名，等价于
    `--scenario 1 --current-step 未知阶段`。--current-step 可进一步覆盖。
    """
    _run_simple_lifecycle(
        args,
        name="Scenario 8",
        subtitle="无效 current_step → MOCK 阶段透传（验证不黑屏）",
        current_step_default="未知阶段",
        extra=("current_step = '未知阶段' → 预期路由到 MOCK stage",),
        tail=("  验证: 后端日志应有 MOCK stage 路由，WebSocket 帧正常推送（无黑屏）",),
    )


# ---------------------------------------------------------------------------
# Scenario 9: current_step 切换（1→2）— 验证二次 start 触发全量重建
# ---------------------------------------------------------------------------


def run_scenario_9(args):
    """
    同一任务先以 current_step=1（LEAK）启动，运行一段时间后将 DB 中 current_step 改为 2，
    再次调用 /api/start，验证：
      1. 第二次 start 不会幂等返回（step 变化触发全量重建）
      2. 后端切换到 CLEAN stage（stage 字段由 InferenceManager 根据 current_step 路由）
      3. 流保持连续推送，两次 start 都成功

    验证点（后端日志关键字）：
      第一次：InferWorker-LEAK 线程正常运行
      第二次：'performing full cleanup before restart'
              InferWorker-CLEAN 线程正常运行
    """
    phase1 = max(15, int(args.duration * 0.4))
    phase2 = max(15, args.duration - phase1)
    section("Scenario 9: current_step 切换（LEAK → CLEAN）",
            "第一次 start: current_step=1 → LEAK stage",
            "DB 更新 current_step → 2",
            "第二次 start: 相同 task_id → 应触发全量重建 → CLEAN stage")

    with scenario_setup(args, current_step="1") as ctx:
        try:
            # ── Phase 1: LEAK 阶段 ──────────────────────────────────────────
            stream_stabilize(ctx.ffmpeg, ctx.is_remote)

            print(f"\n[Step 1] /api/start（current_step=1，预期 LEAK stage）")
            result1 = ctx.api.unified_start(args.task_id, ctx.pull_url, args.fps)
            if "error" in result1:
                raise RuntimeError(f"第一次 /api/start 失败: {result1['error']}")
            print(f"  响应: {result1}")

            # 确认进入 LEAK stage
            status = ctx.api._make_request("GET", "/health/status")
            client_stage = (
                status.get("queues", {})
                      .get(ctx.client_id, {})
                      .get("stage", "unknown")
            )
            print(f"  当前 stage = {client_stage!r}（预期 LEAK）")

            print(f"\n[Step 2] LEAK 阶段运行 {phase1}s...")
            watch_or_sleep(args, ctx.client_id, phase1)

            # ── 切换 current_step ───────────────────────────────────────────
            print(f"\n[Step 3] 更新 DB: current_step 1 → 2")
            ok = DatabaseHelper.update_task_step(args.task_id, "2")
            if not ok:
                raise RuntimeError("更新 current_step 失败，请检查 DB 连接")

            # ── Phase 2: 二次 start，预期全量重建为 CLEAN ───────────────────
            print(f"\n[Step 4] /api/start（current_step 已变为 2，预期触发全量重建）")
            result2 = ctx.api.unified_start(args.task_id, ctx.pull_url, args.fps)
            if "error" in result2:
                raise RuntimeError(f"第二次 /api/start 失败: {result2['error']}")
            print(f"  响应: {result2}")

            # 验证不是幂等返回
            is_idempotent = "idempotent" in result2.get("message", "")
            if is_idempotent:
                print("  [FAIL] 第二次 start 返回了幂等响应，stage 未切换")
            else:
                print("  [PASS] 第二次 start 触发了全量重建（非幂等）")

            # 确认切换到 CLEAN stage
            time.sleep(2)  # 等待 actor 启动
            status2 = ctx.api._make_request("GET", "/health/status")
            client_stage2 = (
                status2.get("queues", {})
                       .get(ctx.client_id, {})
                       .get("stage", "unknown")
            )
            print(f"  当前 stage = {client_stage2!r}（预期 CLEAN）")
            if client_stage2 == "CLEAN":
                print("  [PASS] stage 已切换为 CLEAN")
            else:
                print(f"  [WARN] stage = {client_stage2!r}，非预期的 CLEAN，请检查路由配置")

            print(f"\n[Step 5] CLEAN 阶段运行 {phase2}s...")
            watch_or_sleep(args, ctx.client_id, phase2)
        finally:
            print("\n[Cleanup]", end=" ")
            do_terminate(ctx.api, ctx.client_id, ctx.ffmpeg)

    print("\nScenario 9 完成")
    print("  预期日志路径:")
    print("  第一次: InferWorker-LEAK 启动")
    print("  第二次: 'performing full cleanup before restart' → InferWorker-CLEAN 启动")


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
  7  CLEAN阶段:       别名 = scenario 1 + current_step=2 → CLEAN stage（验证不黑屏）
  8  MOCK阶段:        别名 = scenario 1 + 无效 current_step → MOCK fallback（验证不黑屏）
  9  阶段切换:        start(step=1/LEAK) → DB改step=2 → start again → 全量重建 → CLEAN stage

提示: --current-step 可覆盖任意场景的默认阶段，
      例如 --scenario 2 --current-step 2 测「CLEAN 阶段 + 断流重连」。
        """,
    )
    parser.add_argument("--scenario", type=int, required=True, choices=[1, 2, 3, 4, 5, 6, 7, 8, 9], help="测试场景编号")
    parser.add_argument("--server", default="localhost", help="服务器地址（默认: localhost）")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT, dest="api_port", help=f"后端 API 端口（默认: {DEFAULT_API_PORT}）")
    parser.add_argument("--rtsp-port", type=int, default=DEFAULT_RTSP_PORT, dest="rtsp_port", help=f"RTSPProxy 推流端口（默认: {DEFAULT_RTSP_PORT}）")
    parser.add_argument("--task_id", type=int, required=True, help="任务 ID")
    parser.add_argument(
        "--current-step",
        default=None,
        dest="current_step",
        help="任务 current_step（决定推理 workflow：1=LEAK / 2=CLEAN / 其它=MOCK）。"
             "默认随场景（1-6→1，7→2，8→MOCK）；显式指定可覆盖场景默认，"
             "实现「任意阶段 × 任意生命周期」自由组合。",
    )
    parser.add_argument("--duration", type=int, default=60, help="运行时长（秒，默认: 60）")
    parser.add_argument("--video_path", default=None, help="测试视频路径（默认: test/test_video.mp4）")
    parser.add_argument("--fps", type=int, default=30, help="推流帧率（默认: 30）")
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

    # 端口随 args 透传（args.api_port / args.rtsp_port），不再使用模块级全局

    dispatch = {
        1: run_scenario_1,
        2: run_scenario_2,
        3: run_scenario_3,
        4: run_scenario_4,
        5: run_scenario_5,
        6: run_scenario_6,
        7: run_scenario_7,
        8: run_scenario_8,
        9: run_scenario_9,
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
