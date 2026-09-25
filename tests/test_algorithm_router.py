"""算法路由（/algorithm/*）端到端测试

覆盖 `POST /algorithm/colorstrip`：
- 合成图判定（合格 / 不合格）
- 拒判走 200 + ok=false + code，不是 400
- 请求层问题走 400：空串、非法 base64、非图片字节、超上限、档名写错
- data URL 前缀能吃

**图是合成的，不依赖 28 MB 真实样本**（那些在 `app/services/temp/colorstrip/`，
不入库）。色值按 REPORT.md §2.5 的实测区间取：试纸垫 L*≈8/色相≈25°、
2000 块 L*≈40/色相≈47°、800 块 L*≈60/色相≈66°。

本文件只管"路由接得对不对"。算法判据本身的回归在那份工装的 `acceptance.py`（72 用例）。
"""

import base64
import math

import cv2
import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app

URL = "/algorithm/colorstrip"

# (L*, 色相角°, 彩度) —— 取自 REPORT.md §2.5 各角色实测区间的中间位置
STRIP = (8.0, 25.0, 22.0)
CARD_2000 = (40.0, 47.0, 35.0)
CARD_800 = (60.0, 66.0, 41.0)


def _bgr(lab_polar):
    """(L*, 色相角, 彩度) -> BGR 三元组"""
    L, hue, C = lab_polar
    a = C * math.cos(math.radians(hue))
    b = C * math.sin(math.radians(hue))
    px = np.uint8([[[round(L * 255 / 100), round(a + 128), round(b + 128)]]])
    return tuple(int(v) for v in cv2.cvtColor(px, cv2.COLOR_LAB2BGR)[0, 0])


def _canvas(with_800=True, with_2000=True, strip=STRIP):
    """白底上画：色卡两块上下紧邻等大，试纸在左边离得远。

    几何照着真色卡的成对结构来——竖直对齐、紧邻、等大、ΔL*=20（落在 [10,25]）。
    试纸放左边是为了让"卡×纸"配对卡在 align 上，凑不成第二组色卡对。
    """
    img = np.full((1200, 900, 3), 255, np.uint8)
    if with_800:
        img[300:420, 540:660] = _bgr(CARD_800)
    if with_2000:
        img[440:560, 540:660] = _bgr(CARD_2000)
    if strip is not None:
        img[400:500, 180:280] = _bgr(strip)
    return img


def _b64(img, ext=".png"):
    """PNG 编码（不是 JPEG）：有损压缩会把色块的 L*/色相挪几个单位，
    判据余量虽然够用，但没必要让用例结论挂在编码器上。"""
    ok, buf = cv2.imencode(ext, img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# 判定成立
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_when_strip_darker_than_reference(client):
    r = await client.post(URL, json={"image_base64": _b64(_canvas())})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["passed"] is True
    assert body["code"] == "OK"
    assert body["message"]


@pytest.mark.asyncio
async def test_fail_when_strip_lighter_than_reference(client):
    """试纸比 2000 块浅 -> 不合格。注意这是 ok=true 的正常判定，不是拒判。"""
    lighter = (50.0, 52.0, 34.0)      # L*50 > 参考 40；色相仍在试纸不设门的区间外侧
    r = await client.post(URL, json={"image_base64": _b64(_canvas(strip=lighter))})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["passed"] is False
    assert body["code"] == "OK"


@pytest.mark.asyncio
async def test_accepts_data_url_prefix(client):
    """前端 FileReader.readAsDataURL 直出的就是这个形状，不该要求调用方剥前缀。"""
    payload = "data:image/png;base64," + _b64(_canvas())
    r = await client.post(URL, json={"image_base64": payload})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# 拒判：200 + ok=false，不是 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_800_block_is_rejected_not_400(client):
    """只拍到 2000 一块：参考虽在画面里但无法正向确认，拒判。

    这是接口契约的关键一条——"拍得不对"给 200 + code，调用方判分支看 ok 不看 status。
    """
    r = await client.post(URL, json={"image_base64": _b64(_canvas(with_800=False))})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["passed"] is None
    assert body["code"] == "E_NO_CARD"
    assert "色卡" in body["message"]


@pytest.mark.asyncio
async def test_no_card_at_all_reports_too_few_patches(client):
    r = await client.post(
        URL, json={"image_base64": _b64(_canvas(with_800=False, with_2000=False))}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "E_TOO_FEW_PATCHES"


@pytest.mark.asyncio
async def test_no_strip_reports_strip_count(client):
    r = await client.post(URL, json={"image_base64": _b64(_canvas(strip=None))})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "E_STRIP_COUNT"


# ---------------------------------------------------------------------------
# 请求层问题：400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, why",
    [
        ("", "空串"),
        ("   ", "全空白"),
        ("!!!not-base64!!!", "非法 base64"),
        (base64.b64encode(b"this is not an image").decode(), "合法 base64 但不是图片"),
        ("data:image/png,noseparator", "data URL 缺 ;base64, 分隔符"),
    ],
)
async def test_bad_image_payload_is_400(client, payload, why):
    r = await client.post(URL, json={"image_base64": payload})
    assert r.status_code == 400, f"{why} 应当 400，实际 {r.status_code}"


@pytest.mark.asyncio
async def test_oversized_image_is_400(client):
    """超上限要在**解码前**就拒掉，不能先把整张图吃进内存。"""
    from app.algorithm.colorstrip import config as cs_config

    limit = cs_config.limits()["max_image_bytes"]
    oversized = "A" * (limit * 4 // 3 + 8)     # base64 膨胀 4/3，构造刚好越界的长度
    r = await client.post(URL, json={"image_base64": oversized})
    assert r.status_code == 400
    assert "上限" in r.json().get("error", "") + r.json().get("detail", "")


@pytest.mark.asyncio
async def test_unknown_profile_is_400(client):
    r = await client.post(
        URL, params={"profile": "no-such-profile"},
        json={"image_base64": _b64(_canvas())},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_known_profile_is_accepted(client):
    """params.yaml 里现成的档能被 ?profile= 选中（这里只验档能加载，不验判定结论）。"""
    r = await client.post(
        URL, params={"profile": "low_res"}, json={"image_base64": _b64(_canvas())}
    )
    assert r.status_code == 200
