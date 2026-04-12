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

from app.utils.gateway import IPWhitelistStore, RateLimitStore

logger = logging.getLogger(__name__)

_CHUNK = 65536


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
    ) -> None:
        self._listen_port = listen_port
        self._target_port = target_port
        self._whitelist = whitelist
        self._ratelimit = ratelimit
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle,
            "0.0.0.0",
            self._listen_port,
        )
        logger.info(
            "[RTSPProxy] Listening on :%d → 127.0.0.1:%d",
            self._listen_port,
            self._target_port,
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peername = writer.get_extra_info("peername")
        ip: str = peername[0] if peername else "unknown"

        if not self._whitelist.is_allowed(ip):
            logger.warning("[RTSPProxy] Blocked (whitelist/ban): %s", ip)
            _abort(writer)
            return

        if not self._ratelimit.is_allowed(ip):
            logger.warning("[RTSPProxy] Blocked (rate limit): %s", ip)
            _abort(writer)
            return

        # MediaMTX 可能比代理晚几秒就绪（启动竞争）或短暂重启中，重试最多 10 次
        _RETRIES = 10
        _RETRY_DELAY = 0.5  # 秒，总等待上限 5s
        target_reader = target_writer = None
        for attempt in range(_RETRIES):
            try:
                target_reader, target_writer = await asyncio.open_connection(
                    "127.0.0.1", self._target_port
                )
                break
            except ConnectionRefusedError:
                if attempt < _RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAY)
                else:
                    logger.warning(
                        "[RTSPProxy] MediaMTX unreachable on 127.0.0.1:%d after %d attempts",
                        self._target_port, _RETRIES,
                    )
                    _abort(writer)
                    return

        await asyncio.gather(
            _pipe(reader, target_writer),
            _pipe(target_reader, writer),
            return_exceptions=True,
        )

    def close(self) -> None:
        if self._server:
            self._server.close()
            logger.info("[RTSPProxy] Stopped")
