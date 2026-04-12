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
    s.gateway_health_rate_limit = 300
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
        middleware._health_ratelimit = None
        middleware._antiscan = None
    yield


def _find_gateway_middleware() -> GatewayMiddleware | None:
    """遍历 Starlette 中间件栈，找到 GatewayMiddleware 实例"""
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


# ---------------------------------------------------------------------------
# Unit tests: AntiScanStore
# ---------------------------------------------------------------------------


class TestAntiScanStore:
    def _make_stores(self, threshold=3, window=60, ban_duration=60):
        whitelist = IPWhitelistStore(allowed=frozenset(), ban_duration=ban_duration)
        antiscan = AntiScanStore(threshold=threshold, window=window, whitelist_store=whitelist)
        return whitelist, antiscan

    def test_404_triggers_ban_on_threshold(self):
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

    async def test_health_path_uses_relaxed_limit(self):
        gw = _find_gateway_middleware()
        assert gw is not None

        # rate_limit=2（普通路径很紧），health_rate_limit=100（宽松）
        _init_gw(gw, _make_settings(gateway_rate_limit=2, gateway_health_rate_limit=100))
        async with await self._client("127.0.0.1") as client:
            for _ in range(5):
                resp = await client.get("/health/status")
        # /health/status 用宽松桶，不应该 429
        assert resp.status_code != 429

    async def test_scan_triggers_ban(self):
        gw = _find_gateway_middleware()
        assert gw is not None

        _init_gw(gw, _make_settings(
            gateway_scan_threshold=3,
            gateway_scan_window=60,
            gateway_ban_duration=3600,
            gateway_rate_limit=1000,
        ))
        async with await self._client("127.0.0.1") as client:
            # 触发 3 次 404，达到反扫描阈值
            for _ in range(3):
                await client.get("/nonexistent_path_xyz")
            # IP 已封禁，下一次请求应返回 403
            resp = await client.get("/health/status")
        assert resp.status_code == 403


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
    )
    gw._health_ratelimit = RateLimitStore(
        limit=mock_settings.gateway_health_rate_limit,
        window=mock_settings.gateway_rate_window,
    )
    gw._antiscan = AntiScanStore(
        threshold=mock_settings.gateway_scan_threshold,
        window=mock_settings.gateway_scan_window,
        whitelist_store=gw._whitelist,
    )
    gw._initialized = True
