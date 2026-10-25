"""静态资产挂载：`/ui-f3m8` 一个挂载出 admin / lab / console 三页与共用 vendor。"""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app

_STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
_VENDOR = [
    "vue.global.prod.js", "element-plus.full.js", "element-plus.css", "chart.umd.js", "hls.js",
]


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["admin", "lab", "console"])
async def test_pages_served(client, page):
    r = await client.get(f"/ui-f3m8/{page}/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _VENDOR)
async def test_vendor_served(client, name):
    r = await client.get(f"/ui-f3m8/vendor/{name}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_console_modules_served_as_javascript(client):
    """控制台是 `<script type="module">`：MIME 不是 JavaScript 时浏览器直接拒绝执行，页面白屏。"""
    modules = sorted((_STATIC_DIR / "console").rglob("*.js"))
    assert modules
    for path in modules:
        rel = path.relative_to(_STATIC_DIR).as_posix()
        r = await client.get(f"/ui-f3m8/{rel}")
        assert r.status_code == 200, rel
        assert "javascript" in r.headers["content-type"], rel


@pytest.mark.parametrize("page", ["admin", "lab", "console"])
def test_pages_have_no_private_vendor_dir(page):
    """库文件只放共用的 `static/vendor/`，各页不再有私有 vendor 目录。"""
    assert not (_STATIC_DIR / page / "vendor").exists()
