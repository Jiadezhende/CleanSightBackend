"""
API Gateway 中间件

提供三层防护：
1. IP 白名单 + 动态封禁
2. 滑动窗口速率限制（per-IP）；持续超限自动封禁
3. 反扫描检测（405 计数，触发自动封禁）

封禁触发条件：
  - 404/405（路径枚举 + 方法枚举扫描）：窗口内达到 scan_threshold 次 → 封禁
  - 速率持续超限：窗口内超限违规达到 rate_ban_threshold 次 → 封禁
  - 单次 404/405 不封禁；threshold 参数决定容忍边界

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
    per-IP 滑动窗口速率限制，支持持续超限升级封禁。

    - 单次超限 → 返回 False（调用方返回 429）
    - ban_window 内违规次数 >= ban_threshold → 触发 ban_store.ban(ip)
    - ban_threshold=0 或 ban_store=None 时禁用封禁升级
    """

    def __init__(
        self,
        limit: int,
        window: int,
        ban_store: "IPWhitelistStore | None" = None,
        ban_threshold: int = 0,
        ban_window: int = 60,
    ) -> None:
        self._limit = limit
        self._window = window
        self._buckets: dict[str, deque] = {}
        self._ban_store = ban_store
        self._ban_threshold = ban_threshold
        self._ban_window = ban_window
        self._violations: dict[str, deque] = {}
        self._lock = threading.Lock()
        threading.Thread(
            target=self._cleanup_loop, daemon=True, name="RateLimitStore-cleanup"
        ).start()

    def is_allowed(self, ip: str) -> bool:
        with self._lock:
            now = time.monotonic()
            bucket = self._buckets.setdefault(ip, deque())

            # 弹出窗口外的时间戳
            cutoff = now - self._window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self._limit:
                should_ban = self._track_violation_locked(ip, now)
            else:
                bucket.append(now)
                return True

        # 锁已释放，安全调用 ban（ban_store 有自己的锁）
        if should_ban:
            self._ban_store.ban(ip)  # type: ignore[union-attr]
            logger.warning(
                "[Gateway] Rate-limit ban: %s (%d violations in %ds)",
                ip, self._ban_threshold, self._ban_window,
            )
        return False

    def _track_violation_locked(self, ip: str, now: float) -> bool:
        """在锁内调用，记录超限违规，返回是否应触发封禁。"""
        if not self._ban_store or self._ban_threshold <= 0:
            return False

        vbucket = self._violations.setdefault(ip, deque())
        cutoff = now - self._ban_window
        while vbucket and vbucket[0] < cutoff:
            vbucket.popleft()

        vbucket.append(now)

        if len(vbucket) >= self._ban_threshold:
            self._violations.pop(ip, None)  # 重置计数，ban 后从零开始
            return True
        return False

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(self._window)
            self._sweep()

    def _sweep(self) -> None:
        """驱逐所有窗口内已无活跃记录的 IP key，防止字典无限增长。"""
        now = time.monotonic()
        with self._lock:
            cutoff_b = now - self._window
            cutoff_v = now - self._ban_window
            stale_b = [ip for ip, dq in self._buckets.items() if not dq or dq[-1] < cutoff_b]
            for ip in stale_b:
                del self._buckets[ip]
            stale_v = [ip for ip, dq in self._violations.items() if not dq or dq[-1] < cutoff_v]
            for ip in stale_v:
                del self._violations[ip]


# ============================================================================
# Store: 反扫描检测
# ============================================================================


class AntiScanStore:
    """
    追踪每个 IP 的 404/405 错误数量（路径枚举 + 方法枚举扫描检测）。
    窗口内错误数达到 threshold 时，调用 whitelist_store.ban(ip)。

    404 必须追踪：扫描器的典型手法就是批量探测路径（/admin.asp、/index.php 等），
    均返回 404。threshold 决定了容忍边界——偶发的客户端配置错误（1-2 次）不会
    触发封禁，而短时间大量 404 则是扫描特征。
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
        threading.Thread(
            target=self._cleanup_loop, daemon=True, name="AntiScanStore-cleanup"
        ).start()

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

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(self._window)
            self._sweep()

    def _sweep(self) -> None:
        """驱逐所有窗口内已无活跃记录的 IP key，防止字典无限增长。"""
        now = time.monotonic()
        with self._lock:
            cutoff = now - self._window
            stale = [ip for ip, dq in self._errors.items() if not dq or dq[-1] < cutoff]
            for ip in stale:
                del self._errors[ip]


# ============================================================================
# ASGI 中间件
# ============================================================================


class GatewayMiddleware:
    """
    原始 ASGI 中间件，提供 IP 白名单、速率限制、反扫描三层防护。

    注册方式（app/main.py）：
        app.add_middleware(GatewayMiddleware)
    Starlette 逆序包装，最后注册的最先执行，因此注册在 CORSMiddleware 之后即可。

    宽松路径（relaxed paths）：
      匹配 gateway_relaxed_prefixes 前缀的路径使用宽松速率 bucket，且不计入
      反扫描检测和封禁升级。用于高频轮询接口（/health/、/task/message/ 等），
      避免正常业务调用被误封。
    """

    def __init__(self, app) -> None:
        self._app = app
        self._initialized = False
        self._init_lock = threading.Lock()

        self._whitelist: IPWhitelistStore | None = None
        self._ratelimit: RateLimitStore | None = None
        self._relaxed_ratelimit: RateLimitStore | None = None
        self._relaxed_prefixes: tuple[str, ...] = ()
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
                ban_store=self._whitelist,
                ban_threshold=s.gateway_rate_ban_threshold,
                ban_window=s.gateway_rate_ban_window,
            )
            # 宽松路径：高频轮询接口，不做封禁升级（误伤正常业务调用）
            self._relaxed_ratelimit = RateLimitStore(
                limit=s.gateway_relaxed_rate_limit,
                window=s.gateway_rate_window,
            )
            self._relaxed_prefixes = tuple(
                p.strip() for p in s.gateway_relaxed_prefixes.split(",") if p.strip()
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

        # 判断是否为宽松路径（高频轮询接口）
        is_relaxed = any(path.startswith(prefix) for prefix in self._relaxed_prefixes)

        # 1. IP 白名单 / 封禁检查
        if not self._whitelist.is_allowed(ip):  # type: ignore[union-attr]
            logger.warning("[Gateway] Blocked: %s %s", ip, path)
            await self._send_reject(scope_type, send, 403, "Forbidden", "IP not allowed")
            return

        # 2. 速率限制
        # 宽松路径：独立 bucket，高限额，不做封禁升级
        # 普通路径：标准限额，持续超限升级封禁
        rate_store = self._relaxed_ratelimit if is_relaxed else self._ratelimit  # type: ignore[union-attr]
        if not rate_store.is_allowed(ip):
            logger.warning("[Gateway] Rate limited: %s %s", ip, path)
            await self._send_reject(scope_type, send, 429, "Too Many Requests", "Rate limit exceeded")
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

        # 宽松路径不计入反扫描（高频轮询产生的 404 不是扫描特征）
        if not is_relaxed:
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
    async def _send_reject(
        scope_type: str, send, status: int, error: str, detail: str,
    ) -> None:
        # HTTP 走 JSON body，WebSocket 握手阶段用 websocket.close（policy violation = 1008）
        # 在未 accept 的连接上发送 websocket.close 是 ASGI 合法操作。
        if scope_type == "websocket":
            await GatewayMiddleware._send_ws_close(send, detail)
        else:
            await GatewayMiddleware._send_http_error(send, status, error, detail)

    @staticmethod
    async def _send_http_error(send, status: int, error: str, detail: str) -> None:
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

    @staticmethod
    async def _send_ws_close(send, reason: str) -> None:
        await send({
            "type": "websocket.close",
            "code": 1008,
            "reason": reason,
        })
