"""colorstrip 的私有数据契约与结果码。stdlib only，不 import 同包任何模块。

  from app.services.algorithm.colorstrip.types import Params, OK, E_NO_CARD, message_for
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# ---- 判定结果码 ----
OK = "OK"
E_TOO_FEW_PATCHES = "E_TOO_FEW_PATCHES"    # 色块不足 2 个
E_NO_CARD = "E_NO_CARD"                    # 找不到符合结构与色相窗口的色卡对
E_CARD_AMBIGUOUS = "E_CARD_AMBIGUOUS"      # 多组候选色卡，参考下限有歧义
E_STRIP_COUNT = "E_STRIP_COUNT"            # 待测色块数不等于规范值

ALL_CODES = (OK, E_TOO_FEW_PATCHES, E_NO_CARD, E_CARD_AMBIGUOUS, E_STRIP_COUNT)

# 结果码 → 给操作员看的"现象 + 该怎么办"。与 REPORT.md §4 那张表同源，改一处即可。
# 算法内部 log 里那些带实测值的诊断（"深块色相 48.5°∉[36,56]"）是调参用的，不进这里。
CODE_HINTS = {
    E_TOO_FEW_PATCHES: "画面里没有可比的色块：确认色卡和试纸都拍进去了，别太远、别过曝",
    E_NO_CARD: "未找到色卡：完整色卡两块（800 与 2000）都要入镜，别被反光或瓶身弧面挡住",
    E_CARD_AMBIGUOUS: "画面里有多组疑似色卡：移走其他红橙色物体，只留瓶身色卡和试纸",
    E_STRIP_COUNT: "待测试纸数量不对：确认试纸已显色，且画面里只有 1 条、没混进别的红橙色物体",
}


def message_for(code: str, passed: bool | None, detail: str = "") -> str:
    """结果码 → 一句中文。ok 时给合格与否，拒判时给处置建议。"""
    if code == OK:
        return "合格：试纸比参考色深" if passed else "不合格：试纸比参考色浅"
    hint = CODE_HINTS.get(code)
    if hint and detail:
        return f"{hint}（{detail}）"
    return hint or detail or f"判定失败：{code}"


@dataclass(frozen=True)
class Params:
    """一档参数的解析结果。字段与 params.yaml 的 base 一一对应，扁平化。"""

    profile: str
    # segment
    target_long: int
    hue_band_low_max: int
    hue_band_high_min: int
    sat_min: int
    val_min: int
    val_max: int
    min_area_ratio: float
    morph_kernel: int
    morph_close_iters: int
    # card_pair
    align_max: float
    gap_max: float
    area_min: float
    dl_min: float
    dl_max: float
    # color_window
    hue_2000_min: float
    hue_2000_max: float
    hue_800_min: float
    hue_800_max: float
    strip_hue_max: float
    # spec
    expected_strips: int

    def fingerprint(self):
        """参与判定的全部数值（不含档名）。档名不同但数值相同的两档应当视为等价。"""
        d = asdict(self)
        d.pop("profile")
        return d
