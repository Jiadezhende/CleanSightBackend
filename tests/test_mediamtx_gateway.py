"""
tests/test_mediamtx_gateway.py

MediaMTX Gateway 微服务测试

覆盖范围：
  1. 配置加载 (_load_config) — 默认值、环境变量覆盖
  2. RTSP TCP 代理 — 数据转发、IP 封禁、速率限制、目标不可达
  3. 进程编排 (_run_mediamtx) — 正常退出、stop_event 终止、崩溃重启、超限停止

平台：Windows / Linux 双平台兼容（无 SIGALRM，无 Unix 特有 API，
       asyncio.TimeoutError 作为 Windows ProactorEventLoop 下 EOF 的兜底处理）
"""

import asyncio
import importlib.util
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 加载 scripts/mediamtx_gateway/main.py（无 __init__.py，用 importlib）
# ---------------------------------------------------------------------------

_MOD_PATH = Path(__file__).resolve().parent.parent / "mediamtx_gateway/main.py"
_spec = importlib.util.spec_from_file_location("mediamtx_gateway_main", _MOD_PATH)
_gw_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gw_mod)

_load_config = _gw_mod._load_config
_run_mediamtx = _gw_mod._run_mediamtx
_MAX_RESTARTS = _gw_mod._MAX_RESTARTS

# ---------------------------------------------------------------------------
# RTSP 代理工具（直接导入 app.utils，与 main.py 共享同一实现）
# ---------------------------------------------------------------------------

from app.utils.gateway import IPWhitelistStore, RateLimitStore
from mediamtx_gateway.rtsp_proxy import RTSPProxy


def _make_proxy(
    target_port: int,
    *,
    allowed: frozenset = frozenset(),
    rate_limit: int = 100,
) -> RTSPProxy:
    whitelist = IPWhitelistStore(allowed=allowed, ban_duration=60)
    ratelimit = RateLimitStore(limit=rate_limit, window=60)
    return RTSPProxy(
        listen_port=0,        # OS 自动分配空闲端口
        target_port=target_port,
        whitelist=whitelist,
        ratelimit=ratelimit,
    )


def _proxy_port(proxy: RTSPProxy) -> int:
    return proxy._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


async def _start_echo_server() -> tuple[asyncio.AbstractServer, int]:
    """启动简单 echo 服务，模拟 MediaMTX 内部端口"""

    async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(_echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _read_until_eof(reader: asyncio.StreamReader, timeout: float = 2.0) -> bytes:
    """
    读到 EOF 或连接关闭，返回收到的数据。
    兼容 Windows ProactorEventLoop：捕获 TimeoutError（IOCP 下 EOF 传播延迟）。
    """
    try:
        return await asyncio.wait_for(reader.read(4096), timeout=timeout)
    except (ConnectionResetError, ConnectionAbortedError, asyncio.TimeoutError):
        return b""


# ---------------------------------------------------------------------------
# Mock Process（用于 _run_mediamtx 测试）
# ---------------------------------------------------------------------------


class MockProcess:
    """模拟 asyncio.subprocess.Process"""

    def __init__(self, exit_code: int = 0):
        self.returncode: int | None = None
        self._exit_code = exit_code
        self.stdout = self   # self 作为空 async iterator，模拟无输出进程
        self._done = asyncio.Event()
        self.terminated = False
        self.killed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def wait(self):
        await self._done.wait()
        self.returncode = self._exit_code
        return self._exit_code

    def terminate(self):
        self.terminated = True
        self._done.set()

    def kill(self):
        self.killed = True
        self._done.set()

    def finish(self):
        """模拟进程自然退出"""
        self._done.set()


# ===========================================================================
# 1. TestLoadConfig — 配置加载（纯单元测试）
# ===========================================================================


class TestLoadConfig:
    def test_defaults_no_file_no_env(self, monkeypatch, tmp_path):
        """无 config.ini、无 GATEWAY_* 环境变量 → 全部使用默认值"""
        monkeypatch.setattr(_gw_mod, "_CONFIG_PATH", tmp_path / "nonexistent.ini")
        for key in [
            "GATEWAY_MEDIAMTX_BIN", "GATEWAY_MEDIAMTX_CONFIG",
            "GATEWAY_LISTEN_PORT", "GATEWAY_TARGET_PORT",
            "GATEWAY_ALLOWED_IPS", "GATEWAY_RATE_LIMIT",
            "GATEWAY_RATE_WINDOW", "GATEWAY_BAN_DURATION",
        ]:
            monkeypatch.delenv(key, raising=False)

        conf = _load_config()

        assert conf["mediamtx_bin"] == ""           # 留空 = 纯代理模式
        assert conf["mediamtx_config"] == "mediamtx.yml"
        assert conf["listen_port"] == 8004
        assert conf["target_port"] == 18004
        assert conf["allowed_ips"] == ""
        assert conf["rate_limit"] == 30
        assert conf["rate_window"] == 60
        assert conf["ban_duration"] == 3600

    def test_env_vars_override_defaults(self, monkeypatch, tmp_path):
        """GATEWAY_* 环境变量优先级高于默认值"""
        monkeypatch.setattr(_gw_mod, "_CONFIG_PATH", tmp_path / "nonexistent.ini")
        monkeypatch.setenv("GATEWAY_LISTEN_PORT", "9999")
        monkeypatch.setenv("GATEWAY_RATE_LIMIT", "5")
        monkeypatch.setenv("GATEWAY_ALLOWED_IPS", "10.0.0.1,10.0.0.2")
        monkeypatch.setenv("GATEWAY_BAN_DURATION", "7200")

        conf = _load_config()

        assert conf["listen_port"] == 9999
        assert conf["rate_limit"] == 5
        assert conf["allowed_ips"] == "10.0.0.1,10.0.0.2"
        assert conf["ban_duration"] == 7200
        assert conf["target_port"] == 18004   # 未覆盖的保持默认


# ===========================================================================
# 2. TestRTSPProxy — TCP 代理行为（asyncio 集成测试）
# ===========================================================================


@pytest.mark.asyncio
class TestRTSPProxy:
    async def test_data_forwarded_to_target(self):
        """允许 IP 发送的数据应透明转发到目标，并收到回复"""
        echo_server, target_port = await _start_echo_server()
        proxy = _make_proxy(target_port)
        await proxy.start()
        port = _proxy_port(proxy)

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"RTSP/1.0 OPTIONS\r\n")
            await writer.drain()
            data = await _read_until_eof(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            proxy.close()
            echo_server.close()

        assert data == b"RTSP/1.0 OPTIONS\r\n"

    async def test_whitelist_blocks_unlisted_ip(self):
        """白名单非空且来源 IP 不在其中 → 连接立即关闭（收到 EOF）"""
        echo_server, target_port = await _start_echo_server()
        proxy = _make_proxy(target_port, allowed=frozenset({"10.0.0.1"}))
        await proxy.start()
        port = _proxy_port(proxy)

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            data = await _read_until_eof(reader)
            writer.close()
        finally:
            proxy.close()
            echo_server.close()

        assert data == b""

    async def test_banned_ip_connection_closed(self):
        """已封禁的 IP 发起连接 → 立即关闭"""
        echo_server, target_port = await _start_echo_server()
        whitelist = IPWhitelistStore(allowed=frozenset(), ban_duration=60)
        ratelimit = RateLimitStore(limit=100, window=60)
        whitelist.ban("127.0.0.1")
        proxy = RTSPProxy(0, target_port, whitelist, ratelimit)
        await proxy.start()
        port = _proxy_port(proxy)

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            data = await _read_until_eof(reader)
            writer.close()
        finally:
            proxy.close()
            echo_server.close()

        assert data == b""

    async def test_rate_limit_closes_connection(self):
        """超过速率限制后，新连接应被关闭"""
        echo_server, target_port = await _start_echo_server()
        proxy = _make_proxy(target_port, rate_limit=2)
        await proxy.start()
        port = _proxy_port(proxy)

        try:
            for _ in range(2):
                _, w = await asyncio.open_connection("127.0.0.1", port)
                w.close()
                await w.wait_closed()

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            data = await _read_until_eof(reader)
            writer.close()
        finally:
            proxy.close()
            echo_server.close()

        assert data == b""

    async def test_target_unreachable_closes_gracefully(self):
        """目标端口无服务时，ConnectionRefusedError 被捕获，客户端连接优雅关闭"""
        # 用 socket.bind(0) 获取一个空闲端口号，关闭 socket 后无服务监听
        # 比 asyncio server 方式更可靠，避免关闭时序问题
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            unavailable_port = s.getsockname()[1]

        proxy = _make_proxy(unavailable_port)
        await proxy.start()
        port = _proxy_port(proxy)

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            data = await _read_until_eof(reader)
            writer.close()
        finally:
            proxy.close()

        assert data == b""


# ===========================================================================
# 3. TestRunMediamtx — 进程编排（asyncio，全 mock subprocess）
# ===========================================================================


@pytest.mark.asyncio
class TestRunMediamtx:
    async def test_binary_not_found(self):
        """mediamtx 二进制不存在 → stop_event 被 set"""
        stop = asyncio.Event()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            await _run_mediamtx("nonexistent_binary", "config.yml", stop)
        assert stop.is_set()

    async def test_clean_exit_no_restart(self):
        """进程正常退出（exit code 0）→ 不重启，stop_event 未 set"""
        stop = asyncio.Event()

        async def mock_exec(*args, **kwargs):
            proc = MockProcess(exit_code=0)
            asyncio.get_running_loop().call_soon(proc.finish)
            return proc

        with patch("asyncio.create_subprocess_exec", new=mock_exec):
            await _run_mediamtx("mediamtx", "config.yml", stop)

        assert not stop.is_set()

    async def test_stop_event_terminates_process(self):
        """外部触发 stop_event → 进程被 terminate"""
        stop = asyncio.Event()
        captured: list[MockProcess] = []

        async def mock_exec(*args, **kwargs):
            proc = MockProcess(exit_code=0)   # 不会自行退出
            captured.append(proc)
            return proc

        async def trigger_stop():
            await asyncio.sleep(0)   # 让 _run_mediamtx 进入 asyncio.wait
            stop.set()

        with patch("asyncio.create_subprocess_exec", new=mock_exec):
            await asyncio.gather(
                _run_mediamtx("mediamtx", "config.yml", stop),
                trigger_stop(),
            )

        assert len(captured) == 1
        assert captured[0].terminated

    async def test_crash_triggers_restart(self):
        """进程崩溃（exit code != 0）→ 自动重启，第二次正常退出"""
        stop = asyncio.Event()
        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            exit_code = 1 if call_count == 1 else 0
            proc = MockProcess(exit_code=exit_code)
            asyncio.get_running_loop().call_soon(proc.finish)
            return proc

        async def instant_sleep(*args, **kwargs):
            pass

        with patch("asyncio.create_subprocess_exec", new=mock_exec):
            with patch("asyncio.sleep", new=instant_sleep):
                await _run_mediamtx("mediamtx", "config.yml", stop)

        assert call_count == 2          # 启动了两次（一次崩溃，一次正常退出）
        assert not stop.is_set()        # 正常退出不触发 stop

    async def test_max_restarts_sets_stop_event(self):
        """连续崩溃超过 _MAX_RESTARTS 次 → stop_event 被 set"""
        stop = asyncio.Event()

        async def mock_exec(*args, **kwargs):
            proc = MockProcess(exit_code=1)   # 每次都崩溃
            asyncio.get_running_loop().call_soon(proc.finish)
            return proc

        async def instant_sleep(*args, **kwargs):
            pass

        with patch("asyncio.create_subprocess_exec", new=mock_exec):
            with patch("asyncio.sleep", new=instant_sleep):
                await _run_mediamtx("mediamtx", "config.yml", stop)

        assert stop.is_set()
