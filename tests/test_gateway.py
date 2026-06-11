"""
测试 GatewayMiddleware 三层防护：IP 白名单、速率限制、反扫描检测
"""

import time
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.utils.gateway import AntiScanStore, GatewayMiddleware, IPWhitelistStore, RateLimitStore


# ---------------------------------------------------------------------------
# 工具：构造 mock Settings
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    s = MagicMock()
    s.gateway_enabled = True
    s.allowed_ips_set = frozenset()   # 默认不限制
    s.gateway_rate_limit = 60
    s.gateway_rate_window = 60
    s.gateway_rate_ban_threshold = 5  # 速率超限违规 5 次触发封禁
    s.gateway_rate_ban_window = 60
    s.gateway_relaxed_prefixes = "/health,/task/message"
    s.gateway_relaxed_rate_limit = 600
    s.gateway_bypass_prefixes = "/media"
    s.gateway_scan_threshold = 10
    s.gateway_scan_window = 300
    s.gateway_ban_duration = 3600
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def _reset_gateway():
    """每个测试前重置 GatewayMiddleware 的懒初始化状态"""
    # 找到 app 中注册的 GatewayMiddleware 实例并重置
    middleware = _find_gateway_middleware()
    if middleware:
        middleware._initialized = False
        middleware._whitelist = None
        middleware._ratelimit = None
        middleware._relaxed_ratelimit = None
        middleware._relaxed_prefixes = ()
        middleware._bypass_prefixes = ()
        middleware._antiscan = None
    yield


def _find_gateway_middleware() -> GatewayMiddleware | None:
    """遍历 Starlette 中间件栈，找到 GatewayMiddleware 实例"""
    # middleware_stack 在首次请求前为 None，需手动触发构建
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    current = app.middleware_stack
    while current is not None:
        if isinstance(current, GatewayMiddleware):
            return current
        current = getattr(current, "app", None)
    return None


# ---------------------------------------------------------------------------
# Unit tests: IPWhitelistStore
# ---------------------------------------------------------------------------


class TestIPWhitelistStore:
    def test_empty_whitelist_allows_all(self):
        store = IPWhitelistStore(allowed=frozenset(), ban_duration=60)
        assert store.is_allowed("1.2.3.4")
        assert store.is_allowed("192.168.0.1")

    def test_allowed_ip_passes(self):
        store = IPWhitelistStore(allowed=frozenset({"10.0.0.1"}), ban_duration=60)
        assert store.is_allowed("10.0.0.1")

    def test_blocked_ip_returns_false(self):
        store = IPWhitelistStore(allowed=frozenset({"10.0.0.1"}), ban_duration=60)
        assert not store.is_allowed("1.2.3.4")

    def test_ban_blocks_any_ip(self):
        store = IPWhitelistStore(allowed=frozenset(), ban_duration=60)
        store.ban("5.5.5.5")
        assert not store.is_allowed("5.5.5.5")

    def test_ban_blocks_whitelisted_ip(self):
        store = IPWhitelistStore(allowed=frozenset({"10.0.0.1"}), ban_duration=60)
        store.ban("10.0.0.1")
        assert not store.is_allowed("10.0.0.1")

    def test_ban_expires(self):
        store = IPWhitelistStore(allowed=frozenset(), ban_duration=0)
        store.ban("9.9.9.9")
        # ban_duration=0 → 立即过期
        time.sleep(0.01)
        assert store.is_allowed("9.9.9.9")


# ---------------------------------------------------------------------------
# Unit tests: RateLimitStore
# ---------------------------------------------------------------------------


class TestRateLimitStore:
    def test_within_limit_passes(self):
        store = RateLimitStore(limit=5, window=60)
        for _ in range(5):
            assert store.is_allowed("1.1.1.1")

    def test_exceed_limit_blocked(self):
        store = RateLimitStore(limit=3, window=60)
        for _ in range(3):
            store.is_allowed("2.2.2.2")
        assert not store.is_allowed("2.2.2.2")

    def test_different_ips_independent(self):
        store = RateLimitStore(limit=1, window=60)
        assert store.is_allowed("3.3.3.3")
        assert not store.is_allowed("3.3.3.3")
        assert store.is_allowed("4.4.4.4")  # 另一个 IP 不受影响

    def test_sweep_evicts_stale_buckets(self):
        store = RateLimitStore(limit=5, window=60)
        store.is_allowed("1.2.3.4")
        assert "1.2.3.4" in store._buckets
        # 模拟时间快进超过 window，使 bucket 中的时间戳过期
        with patch("app.utils.gateway.time.monotonic", return_value=time.monotonic() + 120):
            store._sweep()
        assert "1.2.3.4" not in store._buckets

    def test_sweep_evicts_stale_violations(self):
        whitelist = IPWhitelistStore(allowed=frozenset(), ban_duration=3600)
        store = RateLimitStore(
            limit=1, window=60,
            ban_store=whitelist, ban_threshold=5, ban_window=60,
        )
        store.is_allowed("1.2.3.4")  # 消耗配额
        store.is_allowed("1.2.3.4")  # 触发超限，写入 _violations
        assert "1.2.3.4" in store._violations
        with patch("app.utils.gateway.time.monotonic", return_value=time.monotonic() + 120):
            store._sweep()
        assert "1.2.3.4" not in store._violations


# ---------------------------------------------------------------------------
# Unit tests: AntiScanStore
# ---------------------------------------------------------------------------


class TestAntiScanStore:
    def _make_stores(self, threshold=3, window=60, ban_duration=60):
        whitelist = IPWhitelistStore(allowed=frozenset(), ban_duration=ban_duration)
        antiscan = AntiScanStore(threshold=threshold, window=window, whitelist_store=whitelist)
        return whitelist, antiscan

    def test_404_triggers_ban_on_threshold(self):
        # 路径枚举扫描：大量 404 达到阈值后触发封禁
        whitelist, antiscan = self._make_stores(threshold=3)
        for _ in range(3):
            antiscan.record_error("6.6.6.6", 404)
        assert not whitelist.is_allowed("6.6.6.6")

    def test_405_triggers_ban(self):
        whitelist, antiscan = self._make_stores(threshold=2)
        antiscan.record_error("7.7.7.7", 405)
        antiscan.record_error("7.7.7.7", 405)
        assert not whitelist.is_allowed("7.7.7.7")

    def test_200_does_not_count(self):
        whitelist, antiscan = self._make_stores(threshold=2)
        antiscan.record_error("8.8.8.8", 200)
        antiscan.record_error("8.8.8.8", 200)
        assert whitelist.is_allowed("8.8.8.8")

    def test_threshold_zero_disables_ban(self):
        whitelist, antiscan = self._make_stores(threshold=0)
        for _ in range(100):
            antiscan.record_error("9.9.9.9", 404)
        assert whitelist.is_allowed("9.9.9.9")

    def test_below_threshold_no_ban(self):
        whitelist, antiscan = self._make_stores(threshold=5)
        for _ in range(4):
            antiscan.record_error("10.0.0.2", 404)
        assert whitelist.is_allowed("10.0.0.2")

    def test_sweep_evicts_stale_errors(self):
        whitelist, antiscan = self._make_stores(threshold=10, window=60)
        # 触发 3 次 404（不达封禁阈值，key 保留在 _errors 中）
        for _ in range(3):
            antiscan.record_error("10.0.0.3", 404)
        assert "10.0.0.3" in antiscan._errors
        # 模拟时间快进超过 window，使 error 时间戳过期
        with patch("app.utils.gateway.time.monotonic", return_value=time.monotonic() + 120):
            antiscan._sweep()
        assert "10.0.0.3" not in antiscan._errors


# ---------------------------------------------------------------------------
# Integration tests: GatewayMiddleware via httpx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGatewayMiddlewareHTTP:
    async def _client(self, client_ip: str = "127.0.0.1"):
        """返回一个模拟来自 client_ip 的 httpx 客户端"""
        transport = ASGITransport(app=app, client=(client_ip, 9999))
        return AsyncClient(transport=transport, base_url="http://test")

    async def test_gateway_disabled_allows_all(self):
        with patch("app.settings.settings") as mock_settings:
            mock_settings.gateway_enabled = False
            async with await self._client("1.2.3.4") as client:
                resp = await client.get("/health/status")
        assert resp.status_code != 403

    async def test_blocked_ip_returns_403(self):
        gw = _find_gateway_middleware()
        assert gw is not None, "GatewayMiddleware not found in middleware stack"

        _init_gw(gw, _make_settings(allowed_ips_set=frozenset({"10.0.0.1"})))
        async with await self._client("5.5.5.5") as client:
            resp = await client.get("/health/status")
        assert resp.status_code == 403
        assert resp.json()["error"] == "Forbidden"

    async def test_allowed_ip_passes(self):
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(allowed_ips_set=frozenset({"127.0.0.1"})))
        async with await self._client("127.0.0.1") as client:
            resp = await client.get("/health/status")
        assert resp.status_code != 403

    async def test_rate_limit_returns_429(self):
        gw = _find_gateway_middleware()
        assert gw is not None

        # gateway_scan_threshold=100 防止 3 次 405 触发反扫描 ban，干扰限流测试
        _init_gw(gw, _make_settings(
            gateway_rate_limit=3,
            gateway_rate_window=60,
            gateway_scan_threshold=100,
        ))
        async with await self._client("127.0.0.1") as client:
            for _ in range(3):
                await client.get("/api/start")  # GET→405，但限流发生在路由之前
            resp = await client.get("/api/start")
        assert resp.status_code == 429
        assert resp.json()["error"] == "Too Many Requests"

    async def test_relaxed_prefix_uses_relaxed_limit(self):
        gw = _find_gateway_middleware()
        assert gw is not None

        # rate_limit=2（普通路径很紧），relaxed_rate_limit=100（宽松）
        _init_gw(gw, _make_settings(gateway_rate_limit=2, gateway_relaxed_rate_limit=100))
        async with await self._client("127.0.0.1") as client:
            for _ in range(5):
                resp = await client.get("/health/status")
        # /health/status 匹配 /health 前缀，走宽松 bucket，不应 429
        assert resp.status_code != 429

    async def test_task_message_uses_relaxed_limit(self):
        """任务结束后前端持续轮询 /task/message，不应触发限流或封禁"""
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(
            gateway_rate_limit=2,         # 普通路径很紧
            gateway_relaxed_rate_limit=100,
            gateway_scan_threshold=1000,  # 防干扰
        ))
        async with await self._client("127.0.0.1") as client:
            for _ in range(10):
                resp = await client.get("/task/message/123")
        # /task/message/* 走宽松 bucket，不应 429，也不封禁
        assert resp.status_code != 429
        assert resp.status_code != 403

    async def test_bypass_prefix_skips_rate_limit(self):
        """bypass 前缀（如 /media）完全跳过速率限制，token 鉴权由路由层负责"""
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(
            gateway_rate_limit=1,           # 普通路径极紧
            gateway_relaxed_rate_limit=1,   # 宽松也极紧
            gateway_bypass_prefixes="/media",
            gateway_scan_threshold=1000,
        ))
        async with await self._client("127.0.0.1") as client:
            # /media/segment/<bad-token> 会被路由层 403，但中间件不应限流
            for _ in range(20):
                resp = await client.get("/media/segment/fake")
        assert resp.status_code != 429

    async def test_bypass_prefix_skips_antiscan(self):
        """bypass 前缀的 404 不计入反扫描计数，避免合法 token 流量误触发封禁"""
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(
            gateway_scan_threshold=3,
            gateway_scan_window=60,
            gateway_ban_duration=3600,
            gateway_rate_limit=1000,
            gateway_bypass_prefixes="/media",
        ))
        async with await self._client("127.0.0.1") as client:
            # /media/* 的 404/403 应该不计入反扫描
            for _ in range(5):
                await client.get("/media/segment/invalid_token")
            # 仍能正常访问
            resp = await client.get("/health/status")
        assert resp.status_code != 403

    async def test_405_scan_triggers_ban(self):
        """405（方法枚举）达到阈值后触发封禁"""
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(
            gateway_scan_threshold=3,
            gateway_scan_window=60,
            gateway_ban_duration=3600,
            gateway_rate_limit=1000,
        ))
        async with await self._client("127.0.0.1") as client:
            # GET /api/start → 405（该路由只接受 POST），触发 3 次反扫描计数
            for _ in range(3):
                await client.get("/api/start")
            # IP 已封禁，下一次请求应返回 403
            resp = await client.get("/health/status")
        assert resp.status_code == 403

    async def test_404_scan_triggers_ban(self):
        """路径枚举扫描：大量 404 达到阈值后触发封禁"""
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(
            gateway_scan_threshold=3,
            gateway_scan_window=60,
            gateway_ban_duration=3600,
            gateway_rate_limit=1000,
        ))
        async with await self._client("127.0.0.1") as client:
            # 模拟路径枚举：访问 3 个不存在的路径
            for _ in range(3):
                await client.get("/nonexistent_path_xyz")
            # IP 已封禁，下一次请求应返回 403
            resp = await client.get("/health/status")
        assert resp.status_code == 403

    async def test_rate_limit_ban_escalation(self):
        """速率超限持续违规达到阈值后触发封禁"""
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(
            gateway_rate_limit=2,
            gateway_rate_window=60,
            gateway_rate_ban_threshold=3,   # 违规 3 次触发封禁
            gateway_rate_ban_window=60,
            gateway_scan_threshold=1000,    # 防止 405 干扰
        ))
        # 使用 /metrics（普通路径走 _ratelimit，有封禁升级）
        # /health/status 走 _health_ratelimit，没有封禁升级
        async with await self._client("127.0.0.1") as client:
            # 消耗配额（2 次正常）
            for _ in range(2):
                await client.get("/metrics")
            # 连续超限 3 次 → 触发封禁
            for _ in range(3):
                await client.get("/metrics")
            # IP 已封禁，返回 403（不再是 429）
            resp = await client.get("/metrics")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# WebSocket 拒绝路径：直接构造 ASGI scope，断言消息序列
# ---------------------------------------------------------------------------


async def _noop_app(scope, receive, send):
    """下游 app 占位：WS 测试不应触达此处，HTTP 测试也不会"""
    return


def _ws_scope(client_ip: str = "5.5.5.5", path: str = "/ai/video") -> dict:
    return {
        "type": "websocket",
        "path": path,
        "headers": [],
        "client": (client_ip, 12345),
    }


async def _collect_send():
    """返回 (send_callable, messages_list)"""
    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    return send, messages


@pytest.mark.asyncio
class TestGatewayMiddlewareWebSocket:
    async def _receive(self):
        return {"type": "websocket.connect"}

    async def test_blocked_ip_sends_websocket_close(self):
        gw = GatewayMiddleware(_noop_app)
        _init_gw(gw, _make_settings(allowed_ips_set=frozenset({"10.0.0.1"})))

        send, messages = await _collect_send()
        await gw(_ws_scope(client_ip="5.5.5.5"), self._receive, send)

        assert len(messages) == 1
        assert messages[0]["type"] == "websocket.close"
        assert messages[0]["code"] == 1008
        assert messages[0].get("reason") == "IP not allowed"
        # 不应有任何 http.response.* 消息
        assert not any(m["type"].startswith("http.response") for m in messages)

    async def test_rate_limited_sends_websocket_close(self):
        gw = GatewayMiddleware(_noop_app)
        _init_gw(gw, _make_settings(
            gateway_rate_limit=1,
            gateway_rate_window=60,
            gateway_scan_threshold=1000,
            gateway_rate_ban_threshold=1000,  # 避免触发封禁，仅测试限流拒绝
        ))

        # 预先把 rate bucket 塞满，直接命中超限分支
        gw._ratelimit.is_allowed("127.0.0.1")

        send, messages = await _collect_send()
        await gw(_ws_scope(client_ip="127.0.0.1"), self._receive, send)

        assert len(messages) == 1
        assert messages[0]["type"] == "websocket.close"
        assert messages[0]["code"] == 1008
        assert messages[0].get("reason") == "Rate limit exceeded"
        assert not any(m["type"].startswith("http.response") for m in messages)


# ---------------------------------------------------------------------------
# 辅助：手动初始化 GatewayMiddleware（用于测试）
# ---------------------------------------------------------------------------


def _init_gw(gw: GatewayMiddleware, mock_settings) -> None:
    """用 mock settings 初始化 gateway 的各 store"""
    gw._whitelist = IPWhitelistStore(
        allowed=mock_settings.allowed_ips_set,
        ban_duration=mock_settings.gateway_ban_duration,
    )
    gw._ratelimit = RateLimitStore(
        limit=mock_settings.gateway_rate_limit,
        window=mock_settings.gateway_rate_window,
        ban_store=gw._whitelist,
        ban_threshold=mock_settings.gateway_rate_ban_threshold,
        ban_window=mock_settings.gateway_rate_ban_window,
    )
    gw._relaxed_ratelimit = RateLimitStore(
        limit=mock_settings.gateway_relaxed_rate_limit,
        window=mock_settings.gateway_rate_window,
    )
    gw._relaxed_prefixes = tuple(
        p.strip() for p in mock_settings.gateway_relaxed_prefixes.split(",") if p.strip()
    )
    gw._bypass_prefixes = tuple(
        p.strip() for p in mock_settings.gateway_bypass_prefixes.split(",") if p.strip()
    )
    gw._antiscan = AntiScanStore(
        threshold=mock_settings.gateway_scan_threshold,
        window=mock_settings.gateway_scan_window,
        whitelist_store=gw._whitelist,
    )
    gw._initialized = True
