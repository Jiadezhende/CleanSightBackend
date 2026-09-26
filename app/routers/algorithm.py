"""算法 API（`/algorithm/*`）：图进、结论出，不读库不写盘不发告警。

- POST /algorithm/colorstrip —— 过氧乙酸试纸色卡比色

三条改前要知道的约束：

- **端点必须是同步 `def`**，不是 `async def`。函数体整个是 CPU 阻塞活（4.6 MB 手机照
  实测 78 ms，最慢样本 156 ms），留在事件循环上会把同进程的 `/ai/video` 推理画面 WS 一起钉住。
- **算法拒判返 200 + `ok=false`，不是 400**。400 只给请求层问题（base64 解不开、图太大、
  档名写错）；契约见 `docs/api/algorithm.md`，改之前先看那里的「400 与 200+ok:false 的分工」。
- 算法活在 `app.services.algorithm.service`，本层只做请求解析（base64 / data URL → 字节）与
  把它的两个具名异常翻成 400。
"""

from __future__ import annotations

import base64
import binascii
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.algorithm import service as algorithm_service
from app.utils.exceptions import ValidationError

router = APIRouter(prefix="/algorithm", tags=["algorithm"])

_DATA_URL_SEP = ";base64,"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ColorstripRequest(BaseModel):
    image_base64: str = Field(
        ...,
        description="图片的 base64。裸 base64 与 data URL"
        "（`data:image/jpeg;base64,...`）都收——前端 FileReader.readAsDataURL "
        "拿到的就是后者，不必自己剥前缀。",
    )


class ColorstripResponse(BaseModel):
    """四个平铺字段，无嵌套无数组。

    `ok` 与 `passed` 是两件事：`ok=false` 表示**判不出来**（没拍到色卡之类），
    不等于不合格。前端别用 `passed` 反推，判分支看 `ok`。
    """

    ok: bool = Field(..., description="判定是否成立")
    passed: Optional[bool] = Field(
        None, description="合格与否；ok=false 时恒 null"
    )
    code: str = Field(
        ...,
        description="OK / E_TOO_FEW_PATCHES / E_NO_CARD / E_CARD_AMBIGUOUS / E_STRIP_COUNT",
    )
    message: str = Field(..., description="中文一句话，可直接展示给操作员")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _decode_image_base64(raw: str, max_bytes: int) -> bytes:
    """base64（裸串或 data URL）-> 原始字节。任何问题抛 ValidationError（400）。

    先按 base64 膨胀比 4/3 估算解码后大小再真解码——为了拒绝一张超大图而先把它
    整个吃进内存是本末倒置。
    """
    if not raw or not raw.strip():
        raise ValidationError("image_base64 不能为空", field="image_base64")

    payload = raw.strip()
    if payload.startswith("data:"):
        sep = payload.find(_DATA_URL_SEP)
        if sep < 0:
            raise ValidationError(
                "data URL 里找不到 ';base64,' 分隔符，只支持 base64 编码的 data URL",
                field="image_base64",
            )
        payload = payload[sep + len(_DATA_URL_SEP):]
    payload = "".join(payload.split())  # 去掉换行；有些客户端会按 76 列折行

    if len(payload) * 3 // 4 > max_bytes:
        raise ValidationError(
            f"图片超过上限 {max_bytes} 字节（解码前 base64 长度 {len(payload)}）。"
            f"上限在 app/services/algorithm/colorstrip/params.yaml 的 max_image_bytes",
            field="image_base64",
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValidationError(f"image_base64 不是合法的 base64：{e}", field="image_base64")

    if not data:
        raise ValidationError("image_base64 解码后是空数据", field="image_base64")
    if len(data) > max_bytes:
        raise ValidationError(
            f"图片 {len(data)} 字节，超过上限 {max_bytes} 字节", field="image_base64"
        )
    return data


# ---------------------------------------------------------------------------
# 接口 1: 试纸色卡比色
# ---------------------------------------------------------------------------


@router.post("/colorstrip", response_model=ColorstripResponse)
def grade_colorstrip(
    req: ColorstripRequest,
    profile: Optional[str] = Query(
        None,
        description="参数档名（见 app/services/algorithm/colorstrip/params.yaml）；"
        "不传即用该文件的 default_profile。换光照场景才需要动它。",
    ),
) -> ColorstripResponse:
    """一张图里同时拍到瓶身参考色卡与待测试纸，判试纸是否合格。

    做的是**相对比色**：以色卡 2000 刻度块为参考下限，试纸比它更深即合格。同一帧共享
    光照与白平衡，误差大部分相消——所以色卡必须和试纸在同一张图里，两块刻度块都要入镜。
    色卡与试纸谁左谁右、整图转 90°/180° 都不影响判定。

    判不出来时不猜：返回 200 + `ok=false` + `code`，`message` 里写清该怎么补拍。
    """
    data = _decode_image_base64(
        req.image_base64, algorithm_service.colorstrip_max_image_bytes()
    )
    try:
        verdict = algorithm_service.grade_colorstrip(data, profile=profile)
    except algorithm_service.UnknownProfileError as e:
        raise ValidationError(str(e), field="profile")
    except algorithm_service.ImageDecodeError as e:
        raise ValidationError(str(e), field="image_base64")

    return ColorstripResponse(
        ok=verdict.ok, passed=verdict.passed, code=verdict.code, message=verdict.message,
    )
