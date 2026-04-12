"""
MediaMTX Gateway — 独立微服务入口

职责：
  1. 以子进程启动 MediaMTX（可选，mediamtx_bin 留空则跳过）
  2. 在 MediaMTX RTSP 端口前置 TCP 代理（IP 白名单 + 速率限制）
  3. MediaMTX 异常退出时指数退避自动重启（最多 MAX_RESTARTS 次）

配置优先级：环境变量（GATEWAY_*）> config.ini > 默认值

与主后端完全解耦，拥有独立的 Store 实例，各自的 IP 规则互不影响。

启动方式：
  python mediamtx_gateway/main.py
  python -m mediamtx_gateway.main
"""

import asyncio
import configparser
import logging
import os
import signal
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，以便导入 app.utils
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils.gateway import IPWhitelistStore, RateLimitStore
from mediamtx_gateway.rtsp_proxy import RTSPProxy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mediamtx_gateway")

_CONFIG_PATH = Path(__file__).parent / "config.ini"
_MAX_RESTARTS = 5


def _load_config() -> dict:
    cfg = configparser.ConfigParser()
    if _CONFIG_PATH.exists():
        cfg.read(_CONFIG_PATH, encoding="utf-8")

    section = cfg["gateway"] if "gateway" in cfg else {}

    def get(key: str, default, cast=str):
        val = os.environ.get(f"GATEWAY_{key.upper()}") or section.get(key, str(default))
        return cast(val)

    return {
        "mediamtx_bin":    get("mediamtx_bin",    ""),          # 留空 = 不管理 MediaMTX 进程
        "mediamtx_config": get("mediamtx_config",  "mediamtx.yml"),
        "listen_port":     get("listen_port",       8004, int),  # 对外暴露的 RTSP 端口
        "target_port":     get("target_port",       18004, int), # MediaMTX 内部端口
        "allowed_ips":     get("allowed_ips",       ""),         # 逗号分隔，空 = 允许所有
        "rate_limit":      get("rate_limit",        30,   int),  # 滑动窗口内最大连接数
        "rate_window":     get("rate_window",       60,   int),  # 窗口大小（秒）
        "ban_duration":    get("ban_duration",      3600, int),  # 封禁时长（秒）
    }


async def _run_mediamtx(
    bin_path: str,
    config_path: str,
    stop_event: asyncio.Event,
) -> None:
    """启动并守护 MediaMTX 子进程，stop_event 触发后优雅终止。"""
    restarts = 0

    while not stop_event.is_set():
        if restarts > _MAX_RESTARTS:
            logger.critical("[MediaMTX] Max restarts (%d) reached, stopping gateway", _MAX_RESTARTS)
            stop_event.set()
            return

        if restarts > 0:
            delay = min(2 ** restarts, 30)
            logger.warning("[MediaMTX] Restarting in %ds (%d/%d)...", delay, restarts, _MAX_RESTARTS)
            await asyncio.sleep(delay)

        logger.info("[MediaMTX] Starting: %s %s", bin_path, config_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                bin_path, config_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            logger.critical("[MediaMTX] Binary not found: %s", bin_path)
            stop_event.set()
            return

        # 异步转发 MediaMTX 标准输出到本进程日志
        async def _pipe_logs(p):
            async for line in p.stdout:
                logger.info("[MediaMTX] %s", line.decode(errors="replace").rstrip())

        log_task = asyncio.create_task(_pipe_logs(proc))
        wait_task = asyncio.create_task(proc.wait())
        stop_task = asyncio.create_task(stop_event.wait())

        done, pending = await asyncio.wait(
            {wait_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
        log_task.cancel()

        if stop_task in done or stop_event.is_set():
            logger.info("[MediaMTX] Received stop signal, terminating...")
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[MediaMTX] SIGTERM timed out, killing")
                proc.kill()
            return

        exit_code = proc.returncode
        if exit_code == 0:
            logger.info("[MediaMTX] Exited cleanly (code 0)")
            return

        restarts += 1
        logger.error("[MediaMTX] Crashed (exit code %d), restart %d/%d", exit_code, restarts, _MAX_RESTARTS)


async def _main() -> None:
    conf = _load_config()
    logger.info("[Gateway] listen=:%d → 127.0.0.1:%d", conf["listen_port"], conf["target_port"])

    allowed = frozenset(
        ip.strip() for ip in conf["allowed_ips"].split(",") if ip.strip()
    )
    whitelist = IPWhitelistStore(allowed=allowed, ban_duration=conf["ban_duration"])
    ratelimit = RateLimitStore(limit=conf["rate_limit"], window=conf["rate_window"])

    proxy = RTSPProxy(
        listen_port=conf["listen_port"],
        target_port=conf["target_port"],
        whitelist=whitelist,
        ratelimit=ratelimit,
    )

    stop_event = asyncio.Event()

    # 优雅退出信号（SIGINT / SIGTERM）
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows 不支持 loop.add_signal_handler，退回同步 signal
            signal.signal(sig, lambda *_: stop_event.set())

    await proxy.start()

    if conf["mediamtx_bin"]:
        # 管理 MediaMTX 生命周期
        await _run_mediamtx(conf["mediamtx_bin"], conf["mediamtx_config"], stop_event)
    else:
        # 纯代理模式：MediaMTX 由外部管理，只运行 TCP 代理
        logger.info("[Gateway] Proxy-only mode (mediamtx_bin not set)")
        await stop_event.wait()

    proxy.close()
    logger.info("[Gateway] Shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
