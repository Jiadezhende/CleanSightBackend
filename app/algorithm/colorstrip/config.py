"""读同目录 params.yaml，解析参数档。

  cfg = config.load()                # params.yaml 里 default_profile 指定的那一档
  cfg = config.load('warm_light')    # 档不存在直接抛 KeyError，不回退

参数的唯一真源是 [params.yaml](params.yaml)，本模块不留默认值副本——两处都写同一个数，
迟早有一处忘了改。文件缺失或档名写错一律 fail-fast。

本算法包自包含：连"图多大算超限"也在 params.yaml 里（`limits()`），不读 app.settings。
"""

from __future__ import annotations

import argparse
import copy
import functools
import pathlib

import yaml

from .types import Params

CONFIG = pathlib.Path(__file__).parent / "params.yaml"


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


@functools.lru_cache(maxsize=None)
def _doc(path=None):
    p = pathlib.Path(path) if path else CONFIG
    if not p.exists():
        raise FileNotFoundError(f"参数配置缺失: {p}（它是单一真源，代码里没有默认值副本）")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


@functools.lru_cache(maxsize=None)
def load(profile=None, path=None) -> Params:
    """读档。profile 只写覆盖项，其余从 base 继承；None 取 params.yaml 的 default_profile。"""
    doc = _doc(path)
    profile = profile or doc.get("default_profile") or "default"
    available = doc.get("profiles") or {}
    if profile not in available:
        raise KeyError(f"配置里没有档 {profile!r}；现有档: {', '.join(sorted(available))}")
    c = _deep_merge(doc.get("base") or {}, available[profile])

    sg, cp, cw, sp = c["segment"], c["card_pair"], c["color_window"], c["spec"]
    return Params(
        profile=profile,
        target_long=int(sg["target_long"]),
        hue_band_low_max=int(sg["hue_band_low_max"]),
        hue_band_high_min=int(sg["hue_band_high_min"]),
        sat_min=int(sg["sat_min"]),
        val_min=int(sg["val_range"][0]), val_max=int(sg["val_range"][1]),
        min_area_ratio=float(sg["min_area_ratio"]),
        morph_kernel=int(sg["morph_kernel"]),
        morph_close_iters=int(sg["morph_close_iters"]),
        align_max=float(cp["align_max"]),
        gap_max=float(cp["gap_max"]),
        area_min=float(cp["area_min"]),
        dl_min=float(cp["dL_range"][0]), dl_max=float(cp["dL_range"][1]),
        hue_2000_min=float(cw["hue_2000"][0]), hue_2000_max=float(cw["hue_2000"][1]),
        hue_800_min=float(cw["hue_800"][0]), hue_800_max=float(cw["hue_800"][1]),
        strip_hue_max=float(cw["strip_hue_max"]),
        expected_strips=int(sp["expected_strips"]),
    )


def profiles(path=None):
    return sorted((_doc(path).get("profiles") or {}))


def limits(path=None):
    """入参上限。目前只有一项：base64 解码后的原始字节数上限。"""
    return {"max_image_bytes": int(_doc(path).get("max_image_bytes") or 12_000_000)}


def add_profile_arg(parser: argparse.ArgumentParser):
    parser.add_argument("-p", "--profile", default=None,
                        help="参数档名（见 params.yaml），默认取 default_profile")
    return parser


def load_cli(profile):
    """命令行入口专用：档名写错/配置缺失时打一行人话再退出，不甩 traceback"""
    try:
        return load(profile)
    except (KeyError, FileNotFoundError) as e:
        raise SystemExit(f"✗ {e.args[0] if e.args else e}")


def diff(a: Params, b: Params):
    """两档之间有差异的字段，用于门禁报"基线参数与当前参数不符"时点名"""
    fa, fb = a.fingerprint(), b.fingerprint()
    return {k: (fa[k], fb[k]) for k in fa if fa[k] != fb[k]}
