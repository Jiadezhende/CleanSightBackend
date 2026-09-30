"""算法服务对外接口。图（原始字节）进、结论出，不读库不写盘不发告警。

    verdict = grade_colorstrip(image_bytes, profile=None)   # -> ColorstripVerdict
    colorstrip_max_image_bytes()                              # 入参上限，调用方解码前先拦

- **CPU 阻塞**（4.6 MB 手机照实测 78 ms，最慢样本 156 ms）：别在事件循环上直接调。
- **只抛 `ImageDecodeError`（图解不开）/ `UnknownProfileError`（档名不存在）**，调用方
  只接这两个——别宽接 `ValueError` / `KeyError`，那会把算法内部 bug 当成入参错误吞掉。
  算法拒判不是异常，是 `ok=False` 的正常结论。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .colorstrip import config as cs_config
from .colorstrip import grader as cs_grader
from .colorstrip import types as cs_types

logger = logging.getLogger(__name__)


class ImageDecodeError(ValueError):
    """字节解不出图。"""


class UnknownProfileError(KeyError):
    """参数档不存在。str() 直接是人话，不带 KeyError 的引号。"""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


@dataclass(frozen=True)
class ColorstripVerdict:
    """`ok=False` 表示**判不出来**（没拍到色卡之类），不等于不合格；此时 `passed` 恒 None。"""

    ok: bool
    passed: Optional[bool]
    code: str
    message: str


def colorstrip_max_image_bytes() -> int:
    """图片原始字节数上限（params.yaml 的 max_image_bytes）。"""
    return cs_config.limits()["max_image_bytes"]


def grade_colorstrip(image: bytes, profile: Optional[str] = None) -> ColorstripVerdict:
    """一张图里同时拍到瓶身参考色卡与待测试纸，判试纸是否合格。

    profile 为 None 取 params.yaml 的 default_profile。不校验大小——上限用
    `colorstrip_max_image_bytes()` 在解码前拦。
    """
    try:
        cfg = cs_config.load(profile)
    except KeyError as e:
        raise UnknownProfileError(e.args[0] if e.args else str(e)) from e
    try:
        img = cs_grader.imdecode(image)
    except ValueError as e:
        raise ImageDecodeError(str(e)) from e
    res = cs_grader.grade(img, cfg=cfg)

    if res["ok"]:
        # 规范固定 1 条试纸，走到这里 strips 必然恰好 1 条（否则是 E_STRIP_COUNT）
        passed = bool(res["strips"][0]["passed"])
        return ColorstripVerdict(
            ok=True, passed=passed, code=res["code"],
            message=cs_types.message_for(res["code"], passed),
        )

    # 拒判：带实测值的判据诊断只进服务端日志。它是调参用的，对调用方没意义——
    # 调参走 `python -m app.services.algorithm.colorstrip.cli`，排障翻这里的日志。
    logger.info(
        "[AlgorithmService] colorstrip 拒判 code=%s profile=%s\n%s",
        res["code"], cfg.profile, "\n".join(f"  {line}" for line in res["log"]),
    )
    return ColorstripVerdict(
        ok=False, passed=None, code=res["code"],
        message=cs_types.message_for(res["code"], None),
    )
