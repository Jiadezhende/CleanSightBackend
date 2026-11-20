"""
RTSP TCP Gateway

在 MediaMTX RTSP 端口前置轻量 TCP 代理，提供：
1. IP 白名单 / 动态封禁检查
2. 连接速率限制（per-IP）

使用前提：
  mediamtx.yml 中将 rtspAddress 改为 127.0.0.1:18004（仅本地监听）
  代理监听 0.0.0.0:8004 → 转发至 127.0.0.1:18004
"""

import asyncio
import logging

from app.gateway import IPWhitelistStore, RateLimitStore

logger = logging.getLogger(__name__)

_CHUNK = 65536

# MediaMTX 可能比代理晚几秒就绪（启动竞争）或短暂重启中，连目标失败时重试
_CONNECT_RETRIES = 10
_CONNECT_RETRY_DELAY = 0.5  # 秒，总等待上限 5s


def _abort(writer: asyncio.StreamWriter) -> None:
    """强制关闭连接（跨平台）。

    transport.abort() 立即丢弃缓冲区并关闭 socket（发 RST），
    在 Windows ProactorEventLoop 和 Linux SelectorEventLoop 上行为一致：
    对端立即收到 ConnectionResetError，不依赖 FIN 的延迟传播。
    """
    try:
        writer._transport.abort()  # type: ignore[attr-defined]
    except Exception:
        writer.close()  # 降级到优雅关闭


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


class RTSPProxy:
    """
    TCP 代理：IP 检查通过后，将连接透明转发给 MediaMTX 内部端口。
    拒绝时直接关闭连接（RTSP 客户端会看到 Connection Reset）。
    """

    def __init__(
        self,
        listen_port: int,
        target_port: int,
        whitelist: IPWhitelistStore,
        ratelimit: RateLimitStore,
        access_checks: bool = True,
    ) -> None:
        self._access_checks = access_checks
        self._clients = set()
        self._writers = set()
        self._closing = False
        self._listen_port = listen_port
        self._target_port = target_port
        self._whitelist = whitelist
        self._ratelimit = ratelimit
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._closing = False
        self._server = await asyncio.start_server(
            self._handle,
            "0.0.0.0",
            self._listen_port,
        )
        actual_port = self._server.sockets[0].getsockname()[1]
        logger.info(
            "[RTSPProxy] Listening on :%d → 127.0.0.1:%d",
            actual_port,
            self._target_port,
        )

    def _handle(self, reader, writer) -> None:
        if self._closing:
            writer.close()
            return
        task = asyncio.create_task(self._forward(reader, writer))
        self._clients.add(task)
        self._writers.add(writer)
        task.add_done_callback(lambda finished: self._finish_client(finished, writer))

    def _finish_client(self, task, writer) -> None:
        self._clients.discard(task)
        self._writers.discard(writer)
        writer.close()
        if not task.cancelled() and task.exception() is not None:
            logger.warning("[RTSPProxy] Forwarding failed (%s)", type(task.exception()).__name__)

    async def _forward(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peername = writer.get_extra_info("peername")
        ip: str = peername[0] if peername else "unknown"

        if self._access_checks and not self._whitelist.is_allowed(ip):
            logger.warning("[RTSPProxy] Blocked (whitelist/ban): %s", ip)
            _abort(writer)
            return

        if self._access_checks and not self._ratelimit.is_allowed(ip):
            logger.warning("[RTSPProxy] Blocked (rate limit): %s", ip)
            _abort(writer)
            return

        target_reader = target_writer = None
        for attempt in range(_CONNECT_RETRIES):
            try:
                target_reader, target_writer = await asyncio.open_connection(
                    "127.0.0.1", self._target_port
                )
                break
            except ConnectionRefusedError:
                if attempt < _CONNECT_RETRIES - 1:
                    await asyncio.sleep(_CONNECT_RETRY_DELAY)
                else:
                    logger.warning(
                        "[RTSPProxy] MediaMTX unreachable on 127.0.0.1:%d after %d attempts",
                        self._target_port, _CONNECT_RETRIES,
                    )
                    _abort(writer)
                    return

        tasks = [asyncio.create_task(_pipe(reader, target_writer)),
                 asyncio.create_task(_pipe(target_reader, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self.close()
        if self._server:
            await self._server.wait_closed()
            self._server = None
        for writer in tuple(self._writers):
            writer.close()
        tasks = tuple(self._clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        self._closing = True
        if self._server:
            self._server.close()
            logger.info("[RTSPProxy] Stopped")
