"""MediaMTX daemon configuration derived from the shared Settings."""

from dataclasses import dataclass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def resolve_path(value: str) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else ROOT / path)


def resolve_binary(value: str) -> str:
    if not value:
        return ""
    if value.strip().lower() == "auto":
        name = "mediamtx.exe" if os.name == "nt" else "mediamtx"
        return str(ROOT / ".mediamtx" / name)
    return resolve_path(value)


@dataclass(frozen=True)
class MediaMTXConfig:
    binary: str
    config_path: str
    listen_port: int
    target_port: int
    rtp_port: int
    rtcp_port: int
    access_checks: bool
    allowed_ips: frozenset
    rate_limit: int
    rate_window: int
    ban_threshold: int
    ban_window: int
    ban_duration: int


def get_mediamtx_config() -> MediaMTXConfig:
    from app.settings import settings, validate_ports

    validate_ports(settings)
    if (settings.rtsp_rate_limit <= 0 or settings.gateway_rate_window <= 0
            or settings.gateway_rate_ban_window <= 0 or settings.gateway_ban_duration <= 0
            or settings.gateway_rate_ban_threshold < 0):
        raise ValueError("Invalid gateway limits")
    return MediaMTXConfig(
        binary=resolve_binary(settings.mediamtx_bin),
        config_path=resolve_path(settings.mediamtx_config),
        listen_port=settings.mediamtx_proxy_port,
        target_port=settings.mediamtx_internal_port,
        rtp_port=settings.mediamtx_rtp_port,
        rtcp_port=settings.mediamtx_rtcp_port,
        access_checks=settings.gateway_enabled,
        allowed_ips=settings.allowed_ips_set,
        rate_limit=settings.rtsp_rate_limit,
        rate_window=settings.gateway_rate_window,
        ban_threshold=settings.gateway_rate_ban_threshold,
        ban_window=settings.gateway_rate_ban_window,
        ban_duration=settings.gateway_ban_duration,
    )
