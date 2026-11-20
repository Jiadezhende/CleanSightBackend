"""Construct the shared daemon without opening sockets or starting threads."""

from .worker import MediaMTXWorker

mediamtx_worker = MediaMTXWorker()
