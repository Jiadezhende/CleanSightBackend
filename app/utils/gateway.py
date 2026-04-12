"""
API Gateway 中间件

提供三层防护：
1. IP 白名单 + 动态封禁
2. 滑动窗口速率限制（per-IP）
3. 反扫描检测（404/405 计数，触发自动封禁）

实现为原始 ASGI 中间件（非 BaseHTTPMiddleware），避免 WebSocket 升级时的 body buffering 问题。
所有 Store 使用纯 Python + threading.Lock，无外部依赖。
"""

import json
import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)


# ============================================================================
# Store: IP 白名单 + 动态封禁
# ============================================================================


class IPWhitelistStore:
    """
    静态 IP 白名单 + 动态封禁表。

    - allowed 为空集合时，白名单检查关闭（允许所有 IP）
    - ban() 无论白名单是否开启都生效
    """

    def __init__(self, allowed: frozenset, ban_duration: int) -> None:
        self._allowed = allowed
        self._ban_duration = ban_duration
        self._banned: dict[str, float] = {}  # ip -> ban_expiry (monotonic)
        self._lock = threading.Lock()

    def is_allowed(self, ip: str) -> bool:
        with self._lock:
            now = time.monotonic()

            # 清理过期封禁（顺带维护，避免无限增长）
            expired = [k for k, v in self._banned.items() if v <= now]
            for k in expired:
                del self._banned[k]

            # 动态封禁优先于白名单
            if ip in self._banned:
                return False

            # 白名单检查（空集合 = 不限制）
            if self._allowed and ip not in self._allowed:
                return False

            return True

    def ban(self, ip: str) -> None:
        with self._lock:
            self._banned[ip] = time.monotonic() + self._ban_duration
        logger.warning("[Gateway] Auto-banned IP: %s for %ds", ip, self._ban_duration)


# ============================================================================
# Store: 滑动窗口速率限制
# ============================================================================


class RateLimitStore:
    """
    per-IP 滑动窗口速率限制。

    使用 deque 存储请求时间戳，左弹出 O(1)，不超限则追加当前时间。
    """

    def __init__(self, limit: int, window: int) -> None:
        self._limit = limit
        self._window = window
        self._buckets: dict[str, deque] = {}
        self._lock = threading.Lock()

    def is_allowed(self, ip: str) -> bool:
        with self._lock:
            now = time.monotonic()
            bucket = self._buckets.setdefault(ip, deque())

            # 弹出窗口外的时间戳
            cutoff = now - self._window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self._limit:
                return False

            bucket.append(now)
            return True


# ============================================================================
# Store: 反扫描检测
# ============================================================================


class AntiScanStore:
    """
    追踪每个 IP 的 404/405 错误数量。
    窗口内错误数达到 threshold 时，调用 whitelist_store.ban(ip)。
    """

    _TRACKED_CODES = frozenset({404, 405})

    def __init__(
        self,
        threshold: int,
        window: int,
        whitelist_store: IPWhitelistStore,
    ) -> None:
        self._threshold = threshold
        self._window = window
        self._whitelist = whitelist_store
        self._errors: dict[str, deque] = {}
        self._lock = threading.Lock()

    def record_error(self, ip: str, status_code: int) -> None:
        if status_code not in self._TRACKED_CODES:
            return
        if self._threshold <= 0:
            return

        with self._lock:
            now = time.monotonic()
            bucket = self._errors.setdefault(ip, deque())

            cutoff = now - self._window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            bucket.append(now)

            if len(bucket) >= self._threshold:
                self._errors.pop(ip, None)  # 重置计数，ban 后从零开始
                # 在锁外执行 ban（ban 有自己的锁，避免嵌套）
                should_ban = True
            else:
                should_ban = False

        if should_ban:
            self._whitelist.ban(ip)
            logger.warning(
                "[Gateway] Scan detected from %s (%d errors in %ds), banned",
                ip, self._threshold, self._window,
            )


# ============================================================================
# ASGI 中间件
# ============================================================================


class GatewayMiddleware:
    """
    原始 ASGI 中间件，提供 IP 白名单、速率限制、反扫描三层防护。

    注册方式（app/main.py）：
        app.add_middleware(GatewayMiddleware)
    Starlette 逆序包装，最后注册的最先执行，因此注册在 CORSMiddleware 之后即可。
    """

    # 不计入反扫描、使用宽松速率桶的路径
    _HEALTH_PATH = "/health/status"

    def __init__(self, app) -> None:
        self._app = app
        self._initialized = False
        self._init_lock = threading.Lock()

        self._whitelist: IPWhitelistStore | None = None
        self._ratelimit: RateLimitStore | None = None
        self._health_ratelimit: RateLimitStore | None = None
        self._antiscan: AntiScanStore | None = None

    # ------------------------------------------------------------------
    # 懒初始化（双重检查锁，避免循环导入）
    # ------------------------------------------------------------------

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            from app.settings import settings as s

            self._whitelist = IPWhitelistStore(
                allowed=s.allowed_ips_set,
                ban_duration=s.gateway_ban_duration,
            )
            self._ratelimit = RateLimitStore(
                limit=s.gateway_rate_limit,
                window=s.gateway_rate_window,
            )
            self._health_ratelimit = RateLimitStore(
                limit=s.gateway_health_rate_limit,
                window=s.gateway_rate_window,
            )
            self._antiscan = AntiScanStore(
                threshold=s.gateway_scan_threshold,
                window=s.gateway_scan_window,
                whitelist_store=self._whitelist,
            )
            self._initialized = True

    # ------------------------------------------------------------------
    # ASGI 入口
    # ------------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        scope_type = scope.get("type")

        # lifespan 事件直接透传
        if scope_type not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        from app.settings import settings
        if not settings.gateway_enabled:
            await self._app(scope, receive, send)
            return

        self._ensure_initialized()

        ip = self._extract_ip(scope)
        path = scope.get("path", "")

        # 1. IP 白名单 / 封禁检查
        if not self._whitelist.is_allowed(ip):  # type: ignore[union-attr]
            logger.warning("[Gateway] Blocked: %s %s", ip, path)
            await self._send_error(send, 403, "Forbidden", "IP not allowed")
            return

        # 2. 速率限制
        rate_store = (
            self._health_ratelimit
            if path == self._HEALTH_PATH
            else self._ratelimit
        )
        if not rate_store.is_allowed(ip):  # type: ignore[union-attr]
            logger.warning("[Gateway] Rate limited: %s %s", ip, path)
            await self._send_error(send, 429, "Too Many Requests", "Rate limit exceeded")
            return

        # 3. WebSocket 升级直接透传（路由层自行处理鉴权）
        if scope_type == "websocket":
            await self._app(scope, receive, send)
            return

        # 4. HTTP：包装 send 拦截响应状态码，用于反扫描计数
        response_status = [200]

        async def intercepting_send(message):
            if message["type"] == "http.response.start":
                response_status[0] = message["status"]
            await send(message)

        await self._app(scope, receive, intercepting_send)

        # /health/status 不计入反扫描
        if path != self._HEALTH_PATH:
            self._antiscan.record_error(ip, response_status[0])  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_ip(scope) -> str:
        """
        提取客户端真实 IP。
        优先读取 X-Forwarded-For header（为未来加 Nginx 做准备），
        回退到 scope["client"][0]（当前直连模式）。
        """
        headers = dict(scope.get("headers", []))
        forwarded_for = headers.get(b"x-forwarded-for")
        if forwarded_for:
            # 取第一个 IP（最接近客户端）
            return forwarded_for.decode().split(",")[0].strip()

        client = scope.get("client")
        if client:
            return client[0]

        return "unknown"

    @staticmethod
    async def _send_error(send, status: int, error: str, detail: str) -> None:
        """发送 JSON 错误响应（适用于 HTTP 请求和 WebSocket 握手拒绝）"""
        body = json.dumps({"error": error, "detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        })
