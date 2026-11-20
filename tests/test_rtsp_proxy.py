"""RTSP proxy forwarding, access rules and connection cleanup on temporary sockets."""

import asyncio
import socket
import time
from unittest.mock import AsyncMock, Mock

import pytest

from app.gateway import IPWhitelistStore, RateLimitStore  # 与 main.py 共享同一实现
from app.daemons.mediamtx import proxy as rtsp_proxy
from app.daemons.mediamtx.proxy import RTSPProxy


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


_PAYLOAD = b"RTSP/1.0 OPTIONS\r\n"


async def _assert_rejected(port: int, timeout: float = 2.0) -> None:
    """连上代理并先写 payload：断言收不到回显，且远早于读超时就被对端关闭（EOF / RST）。

    只看「读到 b""」分不清被拒和读超时——放行的连接在 echo 回显前超时也是 b""。
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    start = time.monotonic()
    try:
        writer.write(_PAYLOAD)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
    except (ConnectionResetError, ConnectionAbortedError):
        data = b""
    except asyncio.TimeoutError:
        pytest.fail(f"{timeout}s 内连接未被关闭")
    finally:
        writer.close()
    elapsed = time.monotonic() - start
    assert data == b"", f"被拒连接不应收到回显: {data!r}"
    assert elapsed < timeout / 2, f"关闭过慢: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Mock Process（用于 _run_mediamtx 测试）
# ---------------------------------------------------------------------------


# ===========================================================================
# 2. TestRTSPProxy — TCP 代理行为（asyncio 集成测试）
# ===========================================================================


@pytest.mark.asyncio
class TestRTSPProxy:
    async def test_stop_reclaims_client_before_forwarding_first_turn(self, monkeypatch):
        proxy = _make_proxy(18004)
        forward = AsyncMock()
        monkeypatch.setattr(proxy, "_forward", forward)
        writer = Mock()
        proxy._handle(object(), writer)
        assert len(proxy._clients) == 1
        await proxy.stop()
        writer.close.assert_called()
        assert not proxy._clients and not proxy._writers
        forward.assert_not_awaited()
        proxy._ratelimit.close()

    async def test_access_checks_can_be_disabled(self):
        echo_server, target_port = await _start_echo_server()
        store = RateLimitStore(0, 60)
        proxy = RTSPProxy(0, target_port, IPWhitelistStore(frozenset({"192.0.2.1"}), 60),
                          store, access_checks=False)
        await proxy.start()
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", _proxy_port(proxy))
            writer.write(_PAYLOAD)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(4096), 2) == _PAYLOAD
        finally:
            if writer:
                writer.close()
            await proxy.stop()
            store.close()
            echo_server.close()
            await echo_server.wait_closed()

    async def test_stop_closes_active_connections(self):
        echo_server, target_port = await _start_echo_server()
        proxy = _make_proxy(target_port)
        await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", _proxy_port(proxy))
        writer.write(_PAYLOAD)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4096), 2) == _PAYLOAD
        try:
            await asyncio.wait_for(proxy.stop(), 2)
            assert await _read_until_eof(reader) == b""
            assert not proxy._clients and not proxy._writers
        finally:
            writer.close()
            await proxy.stop()
            proxy._ratelimit.close()
            echo_server.close()
            await echo_server.wait_closed()

    async def test_data_forwarded_to_target(self):
        """允许 IP 发送的数据应透明转发到目标，并收到回复"""
        echo_server, target_port = await _start_echo_server()
        proxy = _make_proxy(target_port)
        await proxy.start()
        port = _proxy_port(proxy)

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(_PAYLOAD)
            await writer.drain()
            data = await _read_until_eof(reader)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()
            proxy._ratelimit.close()
            echo_server.close()
            await echo_server.wait_closed()

        assert data == _PAYLOAD

    async def test_whitelist_blocks_unlisted_ip(self):
        """白名单非空且来源 IP 不在其中 → 连接立即关闭，不转发"""
        echo_server, target_port = await _start_echo_server()
        proxy = _make_proxy(target_port, allowed=frozenset({"10.0.0.1"}))
        await proxy.start()

        try:
            await _assert_rejected(_proxy_port(proxy))
        finally:
            await proxy.stop()
            proxy._ratelimit.close()
            echo_server.close()
            await echo_server.wait_closed()

    async def test_banned_ip_connection_closed(self):
        """已封禁的 IP 发起连接 → 立即关闭"""
        echo_server, target_port = await _start_echo_server()
        whitelist = IPWhitelistStore(allowed=frozenset(), ban_duration=60)
        ratelimit = RateLimitStore(limit=100, window=60)
        whitelist.ban("127.0.0.1")
        proxy = RTSPProxy(0, target_port, whitelist, ratelimit)
        await proxy.start()

        try:
            await _assert_rejected(_proxy_port(proxy))
        finally:
            await proxy.stop()
            proxy._ratelimit.close()
            echo_server.close()
            await echo_server.wait_closed()

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

            await _assert_rejected(port)
        finally:
            await proxy.stop()
            proxy._ratelimit.close()
            echo_server.close()
            await echo_server.wait_closed()

    async def test_target_unreachable_closes_gracefully(self, monkeypatch):
        """目标端口无服务时，重试耗尽后 ConnectionRefusedError 被捕获，客户端连接被关闭"""
        # 默认 10 次 × 0.5s 远超读超时；压到 2 次 × 0.01s，才观察得到重试耗尽后的 abort
        monkeypatch.setattr(rtsp_proxy, "_CONNECT_RETRIES", 2)
        monkeypatch.setattr(rtsp_proxy, "_CONNECT_RETRY_DELAY", 0.01)
        # 用 socket.bind(0) 获取一个空闲端口号，关闭 socket 后无服务监听
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            unavailable_port = s.getsockname()[1]

        # Windows 上连本机无监听端口要 ~2s 才报 refused（SYN 重传），2 次就是 4s：
        # 连目标端口直接抛 ConnectionRefusedError，其余连接照常走真实 open_connection
        real_open = asyncio.open_connection
        target_attempts = []

        async def refuse_target(host, port, *args, **kwargs):
            if port == unavailable_port:
                target_attempts.append(port)
                raise ConnectionRefusedError
            return await real_open(host, port, *args, **kwargs)

        monkeypatch.setattr(asyncio, "open_connection", refuse_target)

        proxy = _make_proxy(unavailable_port)
        await proxy.start()

        try:
            await _assert_rejected(_proxy_port(proxy))
        finally:
            await proxy.stop()
            proxy._ratelimit.close()

        assert len(target_attempts) == 2  # 按 _CONNECT_RETRIES 重试后才放弃
