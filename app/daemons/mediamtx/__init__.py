"""RTSP proxy and MediaMTX lifecycle. Consumers use explicit module imports."""

from contextlib import asynccontextmanager

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    from .instance import mediamtx_worker

    await mediamtx_worker.start()
    try:
        yield
    finally:
        await mediamtx_worker.stop()
