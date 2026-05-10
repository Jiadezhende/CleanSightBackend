"""
媒体访问 token：HMAC-SHA256 + 短 TTL 签名

设计目标：
- /media/{kind}/{token} 路由不暴露文件系统路径，token 编码 task_id/step_id/filename/expiry
- 默认 TTL 300s，避免被无限期分发
- 不引入外部缓存：服务端仅做 HMAC 校验，无 token 黑名单
- secret 来自 settings.media_token_secret；为空时启动一次后随机生成进程内临时 secret，
  服务重启会让旧 token 失效（符合预期）

Token 结构：
    payload_b64.signature_b64
    payload = JSON({"t": task_id, "s": step_id, "f": filename, "k": kind, "e": expiry_epoch})
    signature = HMAC-SHA256(secret, payload_bytes)
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Literal, Optional

logger = logging.getLogger(__name__)


MediaKind = Literal["segment", "keypoints"]
_VALID_KINDS = ("segment", "keypoints")


class MediaTokenError(Exception):
    """token 校验失败基类（签名不符 / 过期 / 解码失败）"""


@dataclass(frozen=True)
class MediaTokenPayload:
    """已校验通过的 token 载荷"""

    task_id: int
    step_id: int
    filename: str
    kind: str
    expiry: int


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    # 补齐 padding
    pad = (-len(data)) % 4
    return base64.urlsafe_b64decode(data + "=" * pad)


class MediaToken:
    """媒体 token 签发与校验。

    一般通过 ``MediaToken.default()`` 拿全局单例（secret 取自 settings）。
    测试可直接构造 ``MediaToken(secret=b"...")`` 注入特定密钥。
    """

    _DEFAULT_INSTANCE: "Optional[MediaToken]" = None
    _DEFAULT_LOCK = threading.Lock()

    def __init__(self, secret: bytes, default_ttl: int = 300):
        if not secret:
            raise ValueError("MediaToken secret must not be empty")
        if default_ttl <= 0:
            raise ValueError("default_ttl must be > 0")
        self._secret = secret
        self._default_ttl = default_ttl

    # ------------------------------------------------------------------
    # 单例（按 settings 构造）
    # ------------------------------------------------------------------

    @classmethod
    def default(cls) -> "MediaToken":
        if cls._DEFAULT_INSTANCE is not None:
            return cls._DEFAULT_INSTANCE
        with cls._DEFAULT_LOCK:
            if cls._DEFAULT_INSTANCE is not None:
                return cls._DEFAULT_INSTANCE
            from app.settings import settings

            secret_str = (settings.media_token_secret or "").strip()
            if secret_str:
                secret = secret_str.encode("utf-8")
            else:
                # 未配置 secret：生成进程级临时密钥（重启即失效，告警一次）
                secret = secrets.token_bytes(32)
                logger.warning(
                    "[MediaToken] CLEANSIGHT_MEDIA_TOKEN_SECRET not set; using ephemeral random secret. "
                    "Tokens will be invalidated on restart. Set the env var for stable tokens."
                )
            cls._DEFAULT_INSTANCE = cls(secret=secret, default_ttl=settings.media_token_ttl)
            return cls._DEFAULT_INSTANCE

    @classmethod
    def reset_default(cls) -> None:
        """仅供测试：重置单例，下次 default() 时重新读取 settings"""
        with cls._DEFAULT_LOCK:
            cls._DEFAULT_INSTANCE = None

    # ------------------------------------------------------------------
    # 签发 / 校验
    # ------------------------------------------------------------------

    def sign(
        self,
        task_id: int,
        step_id: int,
        filename: str,
        kind: MediaKind,
        ttl: Optional[int] = None,
        now: Optional[int] = None,
    ) -> str:
        """签发 token。

        Args:
            task_id: 任务 id
            step_id: 洗消步骤 id（来自 clean_task.current_step 转 int）
            filename: 媒体文件名（不含路径，如 "processed_segment_1700000000000000.mp4"）
            kind: "segment" 或 "keypoints"
            ttl: 有效期（秒），默认取构造时的 default_ttl
            now: 当前时间（epoch 秒，用于测试注入）

        Returns:
            URL-safe token 字符串
        """
        if kind not in _VALID_KINDS:
            raise ValueError(f"Invalid kind: {kind!r}, expected one of {_VALID_KINDS}")
        if not filename:
            raise ValueError("filename must be non-empty")
        if "/" in filename or "\\" in filename or filename in (".", ".."):
            raise ValueError(f"Invalid filename (path traversal denied): {filename!r}")

        effective_ttl = ttl if ttl is not None else self._default_ttl
        if effective_ttl <= 0:
            raise ValueError("ttl must be > 0")

        current = now if now is not None else int(time.time())
        expiry = current + effective_ttl

        payload = {
            "t": int(task_id),
            "s": int(step_id),
            "f": filename,
            "k": kind,
            "e": expiry,
        }
        payload_bytes = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        sig = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"

    def verify(
        self,
        token: str,
        kind: Optional[MediaKind] = None,
        now: Optional[int] = None,
    ) -> MediaTokenPayload:
        """校验 token，返回解码后的 payload。

        Args:
            token: 由 sign() 签发的字符串
            kind: 如果提供，校验 token 的 kind 字段必须匹配（防止 segment token 被
                  当 keypoints token 用，反之亦然）
            now: 当前时间（epoch 秒，测试注入）

        Raises:
            MediaTokenError: 格式错误 / 签名不符 / 已过期
        """
        if not token or "." not in token:
            raise MediaTokenError("Malformed token")

        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload_bytes = _b64url_decode(payload_b64)
            sig = _b64url_decode(sig_b64)
        except (ValueError, base64.binascii.Error) as e:  # type: ignore[attr-defined]
            raise MediaTokenError(f"Token decode failed: {e}") from e

        expected_sig = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            raise MediaTokenError("Signature mismatch")

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise MediaTokenError(f"Payload decode failed: {e}") from e

        for k in ("t", "s", "f", "k", "e"):
            if k not in payload:
                raise MediaTokenError(f"Missing field: {k}")

        current = now if now is not None else int(time.time())
        if int(payload["e"]) <= current:
            raise MediaTokenError("Token expired")

        if kind is not None and payload["k"] != kind:
            raise MediaTokenError(f"Kind mismatch: expected {kind}, got {payload['k']}")

        return MediaTokenPayload(
            task_id=int(payload["t"]),
            step_id=int(payload["s"]),
            filename=str(payload["f"]),
            kind=str(payload["k"]),
            expiry=int(payload["e"]),
        )
