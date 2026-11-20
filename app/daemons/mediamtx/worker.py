"""Async owner of the RTSP listener and an optional MediaMTX subprocess."""

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path

from app.gateway import IPWhitelistStore, RateLimitStore
from .config import MediaMTXConfig, get_mediamtx_config
from .proxy import RTSPProxy


logger = logging.getLogger(__name__)
_MAX_RESTARTS = 5
_START_TIMEOUT = 5
_STOP_TIMEOUT = 5


class MediaMTXWorker:
    def __init__(self, config: MediaMTXConfig | None = None):
        self._provided_config = config
        self._config = None
        self._lock = asyncio.Lock()
        self._stop_event = None
        self._proxy = None
        self._ratelimit = None
        self._process = None
        self._log_task = None
        self._supervisor = None
        self._state = "stopped"
        self._restarts = 0
        self._error = None

    def get_status(self) -> dict:
        return {
            "state": self._state,
            "managed": bool(self._config and self._config.binary),
            "restart_count": self._restarts,
            "error_code": self._error,
        }

    async def start(self) -> None:
        async with self._lock:
            if self._state in ("running", "restarting"):
                return
            # Reap an earlier completed supervisor before reusing this instance.
            if self._supervisor:
                await asyncio.gather(self._supervisor, return_exceptions=True)
                self._supervisor = None
            self._config = self._provided_config or get_mediamtx_config()
            self._stop_event = asyncio.Event()
            self._state = "starting"
            self._restarts = 0
            self._error = None
            try:
                config = self._config
                if config.binary:
                    if not Path(config.binary).is_file() or not Path(config.config_path).is_file():
                        raise FileNotFoundError("MediaMTX binary or configuration missing")
                whitelist = IPWhitelistStore(config.allowed_ips, config.ban_duration)
                self._ratelimit = RateLimitStore(
                    config.rate_limit, config.rate_window, ban_store=whitelist,
                    ban_threshold=config.ban_threshold, ban_window=config.ban_window,
                )
                self._proxy = RTSPProxy(
                    config.listen_port, config.target_port, whitelist, self._ratelimit,
                    access_checks=config.access_checks,
                )
                await self._proxy.start()
                if config.binary:
                    await self._spawn()
                    await self._wait_ready()
                    self._supervisor = asyncio.create_task(self._supervise(), name="mediamtx-supervisor")
                self._state = "running"
                logger.info("[MediaMTXWorker] Started, managed=%s", bool(config.binary))
            except BaseException:
                self._state = "failed"
                self._error = "startup_failed"
                await self._cleanup()
                raise

    async def _spawn(self) -> None:
        config = self._config
        environment = dict(os.environ)
        environment.update(
            MTX_RTSPADDRESS=f"127.0.0.1:{config.target_port}",
            MTX_RTPADDRESS=f"127.0.0.1:{config.rtp_port}",
            MTX_RTCPADDRESS=f"127.0.0.1:{config.rtcp_port}",
        )
        self._process = await asyncio.create_subprocess_exec(
            config.binary, config.config_path, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self._log_task = asyncio.create_task(self._pipe_logs(self._process))

    async def _pipe_logs(self, process) -> None:
        async for line in process.stdout:
            logger.info("[MediaMTX] %s", line.decode(errors="replace").rstrip())

    async def _wait_ready(self) -> None:
        async def probe():
            while True:
                if self._process.returncode is not None:
                    raise RuntimeError("MediaMTX exited during startup")
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", self._config.target_port)
                except OSError:
                    await asyncio.sleep(0.05)
                    continue
                writer.close()
                await writer.wait_closed()
                if self._process.returncode is not None:
                    raise RuntimeError("MediaMTX exited during startup")
                return
        await asyncio.wait_for(probe(), timeout=_START_TIMEOUT)

    async def _wait_exit_or_stop(self):
        wait = asyncio.create_task(self._process.wait())
        stop = asyncio.create_task(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait((wait, stop), return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                return None
            return wait.result()
        finally:
            for task in (wait, stop):
                task.cancel()
            await asyncio.gather(wait, stop, return_exceptions=True)

    async def _backoff(self) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), min(2 ** self._restarts, 30))
            return False
        except asyncio.TimeoutError:
            return True

    async def _supervise(self) -> None:
        try:
            while not self._stop_event.is_set():
                code = await self._wait_exit_or_stop()
                if code is None:
                    return
                await self._stop_process()
                if code == 0:
                    await self._fail("unexpected_exit")
                    return
                if self._restarts >= _MAX_RESTARTS:
                    await self._fail("retries_exhausted")
                    return
                self._restarts += 1
                self._state = "restarting"
                if not await self._backoff():
                    return
                await self._spawn()
                await self._wait_ready()
                self._state = "running"
        except asyncio.CancelledError:
            await self._fail("supervision_cancelled")
        except Exception:
            await self._fail("supervision_failed")

    async def _fail(self, code: str) -> None:
        self._state = "failed"
        self._error = code
        logger.error("[MediaMTXWorker] Failed: %s", code)
        await self._cleanup()

    async def _stop_process(self) -> None:
        process = self._process
        if process is not None:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), _STOP_TIMEOUT)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            else:
                await process.wait()
            self._process = None
        if self._log_task:
            self._log_task.cancel()
            await asyncio.gather(self._log_task, return_exceptions=True)
            self._log_task = None

    async def _cleanup(self) -> None:
        if self._proxy:
            await self._proxy.stop()
            self._proxy = None
        if self._ratelimit:
            self._ratelimit.close()
            self._ratelimit = None
        await self._stop_process()

    async def stop(self) -> None:
        async with self._lock:
            if self._stop_event:
                self._stop_event.set()
            if self._supervisor:
                await asyncio.gather(self._supervisor, return_exceptions=True)
                self._supervisor = None
            await self._cleanup()
            self._state = "stopped"
