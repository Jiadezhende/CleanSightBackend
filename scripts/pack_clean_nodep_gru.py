"""把训练框架交付的 CLEAN 滑窗 GRU checkpoint（+ 旁挂 .meta.json）打成自包含部署物料。

Usage:
    python scripts/pack_clean_nodep_gru.py <gru_xxx_best.pt> app/data/clean-offline-gru-nodep.pt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.inference.offline.impl.clean import pack_window_gru_checkpoint  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("src", type=Path, help="训练框架 checkpoint；同目录须有 <src>.meta.json")
    parser.add_argument("dst", type=Path, help="部署物料输出路径")
    args = parser.parse_args()
    pack_window_gru_checkpoint(args.src, args.dst)
    print(f"packed: {args.src} -> {args.dst}")


if __name__ == "__main__":
    main()
