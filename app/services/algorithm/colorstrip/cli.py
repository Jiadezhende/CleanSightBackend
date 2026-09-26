"""对着照片直接跑判定，看判定过程与各判据实测值——换场景调参的主力工具。

  python -m app.services.algorithm.colorstrip.cli 图1.jpg 图2.jpg [-p 档名] [--viz 输出目录]

拒判时会打出每对候选色块卡在哪条判据、实测值多少，照着去改 params.yaml：

  最接近的候选对（共 3 组组合）：
    深块 L*40.8/色相48.5° × 浅块 L*60.8/色相65.9°  卡在 深块色相 48.5°∉[28,48]

规范 §1 的 cli.py 例外条款：本文件是**单向出口**，不得被包内任何其他模块 import。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from . import config, grader


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="对单张/多张图跑色卡比色判定")
    ap.add_argument("images", nargs="+", help="图片路径")
    ap.add_argument("--viz", metavar="DIR", help="把效果图落到该目录")
    config.add_profile_arg(ap)
    a = ap.parse_args(argv)

    cfg = config.load_cli(a.profile)
    out = pathlib.Path(a.viz) if a.viz else None
    if out:
        out.mkdir(parents=True, exist_ok=True)

    worst = 0
    for path in a.images:
        p = pathlib.Path(path)
        viz = out / f"{p.stem}.jpg" if out else None
        res = grader.grade(grader.imread(p), viz_path=viz, cfg=cfg)
        print(f'\n===== {p.name} -> {res["code"]} =====')
        for line in res["log"]:
            print("  " + line)
        if viz:
            print(f"  效果图 -> {viz}")
        worst = max(worst, 0 if res["ok"] else 1)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
