"""
测试 GatewayMiddleware 三层防护：IP 白名单、速率限制、反扫描检测
"""

import time
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.settings import Settings, settings
from app.gateway import AntiScanStore, GatewayMiddleware, IPWhitelistStore, RateLimitStore


# ---------------------------------------------------------------------------
# 工具：把测试配置写进真实 settings，走真实 `_ensure_initialized` 建 store
# ---------------------------------------------------------------------------


# 各用例的基线配置：显式写死，不随 .env.* 漂移；用例只覆盖自己关心的字段
_BASELINE = dict(
    gateway_enabled=True,
    gateway_allowed_ips="",           # 空 = 不限制
    gateway_rate_limit=60,
    gateway_rate_window=60,
    gateway_rate_ban_threshold=5,     # 速率超限违规 5 次触发封禁
    gateway_rate_ban_window=60,
    gateway_relaxed_prefixes="/health,/task/message",
    gateway_relaxed_rate_limit=600,
    gateway_bypass_prefixes="/media",
    gateway_scan_threshold=10,
    gateway_scan_window=300,
    gateway_ban_duration=3600,
)

# 生产默认宽松前缀（Settings 字段默认值，不受 .env.* 覆盖）
_PROD_RELAXED_PREFIXES = Settings.model_fields["gateway_relaxed_prefixes"].default


def _reset(gw: GatewayMiddleware) -> None:
    """清掉懒初始化状态，下次请求 / `_ensure_initialized` 按当前 settings 重建"""
    gw._initialized = False
    gw._whitelist = None
    gw._ratelimit = None
    gw._relaxed_ratelimit = None
    gw._relaxed_prefixes = ()
    gw._bypass_prefixes = ()
    gw._antiscan = None


@pytest.fixture(autouse=True)
def _reset_gateway():
    """前后各重置一次 app 上的 GatewayMiddleware：不吃上个用例的封禁，也不把封禁留给别的测试文件"""
    middleware = _find_gateway_middleware()
    if middleware:
        _reset(middleware)
    yield
    if middleware:
        _reset(middleware)


@pytest.fixture
def init_gw(monkeypatch):
    """`init_gw(gw, **overrides)`：基线 + overrides 写进 settings，再调真实初始化"""

    def _init(gw: GatewayMiddleware, **overrides) -> GatewayMiddleware:
        for key, value in {**_BASELINE, **overrides}.items():
            monkeypatch.setattr(settings, key, value)
        _reset(gw)
        gw._ensure_initialized()
        return gw

    return _init


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
        with patch("app.gateway.time.monotonic", return_value=time.monotonic() + 120):
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
        with patch("app.gateway.time.monotonic", return_value=time.monotonic() + 120):
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

    @pytest.mark.parametrize("status", [404, 405])  # 路径枚举 / 方法枚举
    def test_scan_status_triggers_ban_on_threshold(self, status):
        whitelist, antiscan = self._make_stores(threshold=3)
        for _ in range(3):
            antiscan.record_error("6.6.6.6", status)
        assert not whitelist.is_allowed("6.6.6.6")

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
        with patch("app.gateway.time.monotonic", return_value=time.monotonic() + 120):
            antiscan._sweep()
        assert "10.0.0.3" not in antiscan._errors


# ---------------------------------------------------------------------------
# Unit tests: 后台清理线程
# ---------------------------------------------------------------------------


class TestCleanupThread:
    """守 `_cleanup_loop` 这条线程本身，而非它调用的 _sweep。

    上面的 test_sweep_evicts_* 都是 patch 掉 time.monotonic 后直接调 _sweep()，
    绕开了线程——把 __init__ 里的 Thread(...).start() 删掉，那几个用例照样全绿，
    字典却会在线上无限增长。这里不打桩、不手调 _sweep，写完就干等，
    只让线程按 window 自转来清。
    """

    WINDOW = 0.2    # 线程周期 = window，取 0.2s 让用例跑得快
    N_IPS = 50
    # 首轮 sweep 时刚写入的时间戳还没出窗（cutoff 恰好压在写入时刻上），
    # 要等到第二轮才会被判定为 stale，即约 2×WINDOW。留 3 倍余量抗慢机。
    TIMEOUT = WINDOW * 6

    def test_stores_self_evict_without_manual_sweep(self):
        whitelist = IPWhitelistStore(allowed=frozenset(), ban_duration=3600)
        # 两个 threshold 都按 per-IP 计，本用例每个 IP 只写 1 次违规 / 1 次 404，
        # 设成 10 足以不触发封禁——一旦封禁，对应字典会被 ban 逻辑 pop 掉，
        # 那就分不清是线程清的还是 ban 清的了。
        rate = RateLimitStore(
            limit=1,
            window=self.WINDOW,
            ban_store=whitelist,
            ban_threshold=10,
            ban_window=self.WINDOW,
        )
        antiscan = AntiScanStore(
            threshold=10,
            window=self.WINDOW,
            whitelist_store=whitelist,
        )

        for i in range(self.N_IPS):
            ip = f"10.0.0.{i}"
            rate.is_allowed(ip)             # 首次放行 → 写入 _buckets
            rate.is_allowed(ip)             # 超限 → 写入 _violations
            antiscan.record_error(ip, 404)  # → 写入 _errors

        assert len(rate._buckets) == self.N_IPS
        assert len(rate._violations) == self.N_IPS
        assert len(antiscan._errors) == self.N_IPS

        deadline = time.monotonic() + self.TIMEOUT
        while time.monotonic() < deadline:
            if not (rate._buckets or rate._violations or antiscan._errors):
                return
            time.sleep(0.02)

        pytest.fail(
            f"{self.TIMEOUT}s 内未被清理线程回收："
            f"_buckets={len(rate._buckets)} "
            f"_violations={len(rate._violations)} "
            f"_errors={len(antiscan._errors)}"
        )


# ---------------------------------------------------------------------------
# Integration tests: GatewayMiddleware via httpx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGatewayMiddlewareHTTP:
    async def _client(self, client_ip: str = "127.0.0.1"):
        """返回一个模拟来自 client_ip 的 httpx 客户端"""
        transport = ASGITransport(app=app, client=(client_ip, 9999))
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.fixture
    def gw(self):
        gw = _find_gateway_middleware()
        assert gw is not None, "GatewayMiddleware not found in middleware stack"
        return gw

    async def test_gateway_disabled_allows_all(self, gw, init_gw, monkeypatch):
        # 白名单不含 1.2.3.4：开着会 403（见 test_blocked_ip_returns_403），关掉必须放行
        init_gw(gw, gateway_allowed_ips="10.0.0.1")
        monkeypatch.setattr(settings, "gateway_enabled", False)
        async with await self._client("1.2.3.4") as client:
            resp = await client.get("/health/status")
        assert resp.status_code != 403

    async def test_blocked_ip_returns_403(self, gw, init_gw):
        init_gw(gw, gateway_allowed_ips="10.0.0.1")
        async with await self._client("5.5.5.5") as client:
            resp = await client.get("/health/status")
        assert resp.status_code == 403
        assert resp.json()["error"] == "Forbidden"

    async def test_rate_limit_returns_429(self, gw, init_gw):
        # gateway_scan_threshold=100 防止 3 次 405 触发反扫描 ban，干扰限流测试
        init_gw(gw, gateway_rate_limit=3, gateway_rate_window=60, gateway_scan_threshold=100)
        async with await self._client("127.0.0.1") as client:
            for _ in range(3):
                await client.get("/api/start")  # GET→405，但限流发生在路由之前
            resp = await client.get("/api/start")
        assert resp.status_code == 429
        assert resp.json()["error"] == "Too Many Requests"

    @pytest.mark.parametrize("path", ["/health/status", "/task/message/123", "/admin-f3m8/overview"])
    async def test_relaxed_prefix_uses_relaxed_limit(self, gw, init_gw, path):
        """生产默认宽松前缀下，健康检查、任务结束后前端持续轮询的 /task/message、admin 页数据接口不被限流或封禁"""
        init_gw(
            gw,
            gateway_relaxed_prefixes=_PROD_RELAXED_PREFIXES,
            gateway_rate_limit=2,         # 普通路径很紧：第 3 次即 429
            gateway_relaxed_rate_limit=100,
            gateway_scan_threshold=1000,  # 防干扰
        )
        async with await self._client("127.0.0.1") as client:
            for _ in range(10):
                resp = await client.get(path)
        assert resp.status_code not in (403, 429)

    async def test_bypass_prefix_skips_rate_limit(self, gw, init_gw):
        """bypass 前缀（如 /media）完全跳过速率限制，token 鉴权由路由层负责"""
        init_gw(
            gw,
            gateway_rate_limit=1,           # 普通路径极紧
            gateway_relaxed_rate_limit=1,   # 宽松也极紧
            gateway_bypass_prefixes="/media",
            gateway_scan_threshold=1000,
        )
        async with await self._client("127.0.0.1") as client:
            # /media/segment/<bad-token> 会被路由层 403，但中间件不应限流
            for _ in range(20):
                resp = await client.get("/media/segment/fake")
        assert resp.status_code != 429

    async def test_bypass_prefix_skips_antiscan(self, gw, init_gw):
        """bypass 前缀的 404 不计入反扫描计数，避免合法 token 流量误触发封禁"""
        init_gw(
            gw,
            gateway_scan_threshold=3,
            gateway_scan_window=60,
            gateway_ban_duration=3600,
            gateway_rate_limit=1000,
            gateway_bypass_prefixes="/media",
        )
        async with await self._client("127.0.0.1") as client:
            # /media/* 的 404/403 应该不计入反扫描
            for _ in range(5):
                await client.get("/media/segment/invalid_token")
            # 仍能正常访问
            resp = await client.get("/health/status")
        assert resp.status_code != 403

    @pytest.mark.parametrize(
        "path",
        [
            "/nonexistent_path_xyz",  # 404：路径枚举
            "/api/start",             # 405：方法枚举（该路由只接受 POST）
        ],
    )
    async def test_scan_triggers_ban(self, gw, init_gw, path):
        """404 / 405 达到阈值后触发封禁"""
        init_gw(
            gw,
            gateway_scan_threshold=3,
            gateway_scan_window=60,
            gateway_ban_duration=3600,
            gateway_rate_limit=1000,
        )
        async with await self._client("127.0.0.1") as client:
            for _ in range(3):
                await client.get(path)
            # IP 已封禁，下一次请求应返回 403
            resp = await client.get("/health/status")
        assert resp.status_code == 403

    async def test_rate_limit_ban_escalation(self, gw, init_gw):
        """速率超限持续违规达到阈值后触发封禁"""
        init_gw(
            gw,
            gateway_rate_limit=2,
            gateway_rate_window=60,
            gateway_rate_ban_threshold=3,   # 违规 3 次触发封禁
            gateway_rate_ban_window=60,
            gateway_scan_threshold=1000,    # 防止 405 干扰
        )
        # 普通路径走 _ratelimit，有封禁升级；宽松路径走 _relaxed_ratelimit，没有。
        # /metrics 在生产默认宽松前缀里，这里靠基线前缀（/health,/task/message）当普通路径用
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

    async def test_blocked_ip_sends_websocket_close(self, init_gw):
        gw = init_gw(GatewayMiddleware(_noop_app), gateway_allowed_ips="10.0.0.1")

        send, messages = await _collect_send()
        await gw(_ws_scope(client_ip="5.5.5.5"), self._receive, send)

        assert len(messages) == 1
        assert messages[0]["type"] == "websocket.close"
        assert messages[0]["code"] == 1008
        assert messages[0].get("reason") == "IP not allowed"
        # 不应有任何 http.response.* 消息
        assert not any(m["type"].startswith("http.response") for m in messages)

    async def test_rate_limited_sends_websocket_close(self, init_gw):
        gw = init_gw(
            GatewayMiddleware(_noop_app),
            gateway_rate_limit=1,
            gateway_rate_window=60,
            gateway_scan_threshold=1000,
            gateway_rate_ban_threshold=1000,  # 避免触发封禁，仅测试限流拒绝
        )

        # 预先把 rate bucket 塞满，直接命中超限分支
        gw._ratelimit.is_allowed("127.0.0.1")

        send, messages = await _collect_send()
        await gw(_ws_scope(client_ip="127.0.0.1"), self._receive, send)

        assert len(messages) == 1
        assert messages[0]["type"] == "websocket.close"
        assert messages[0]["code"] == 1008
        assert messages[0].get("reason") == "Rate limit exceeded"
        assert not any(m["type"].startswith("http.response") for m in messages)
