"""生成 nodep-226d 特征的逐位对齐基准（训练框架参考实现 → expected.npz）。

用法（需训练侧交付的参考源码目录，含 clean_bbox_v2.py / clean_bbox_v3.py / nodep_concat.py；
仓库里 ref/ 被 gitignore，故基准产物入库、本脚本仅用于重新生成）：

    python tests/fixtures/clean_nodep_golden/generate.py --ref <ref/ama-v3-concat23-nodep-226d-gru-w16>

产物：input.json（合成检测序列，xyxy 像素 + 置信度）与 expected.npz（conf_default / conf_real 两份 [T,226]）。
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FPS = 7.5
WIDTH, HEIGHT = 640, 480
FRAMES = 72
# 训练 detection_mapping 的 id 口径；bubble 不在 mapping 内，验证未知类被忽略
CLASS_IDS = {
    "hand": 0, "scope_control_body": 1, "scope_mid_section": 2, "scope_distal_end": 3,
    "syringe": 4, "air_gun": 5, "short_brush": 6, "brush_tip_out": 7, "long_brush": 8, "bubble": 9,
}
MAPPING = {i: n for n, i in CLASS_IDS.items() if n != "bubble"}


def _box(rng, cx, cy, w, h):
    x1 = int(np.clip(cx - w / 2, 0, WIDTH - 2))
    y1 = int(np.clip(cy - h / 2, 0, HEIGHT - 2))
    x2 = int(np.clip(cx + w / 2, x1 + 1, WIDTH))
    y2 = int(np.clip(cy + h / 2, y1 + 1, HEIGHT))
    return [x1, y1, x2, y2, round(float(rng.uniform(0.3, 1.0)), 3)]


def make_input() -> dict:
    """覆盖：多 hand 候选、同类多框、短缺口（≤6 帧插值）与长缺口、scope 轴三种回退、废弃类与未知类。"""
    rng = np.random.default_rng(20260927)
    frames = []
    for t in range(FRAMES):
        boxes = []
        for k in range(int(rng.integers(0, 4))):
            boxes.append(["hand", *_box(rng, 200 + 80 * k + 3 * t, 300 - 2 * t, 60, 50)])
        if not (12 <= t < 15 or 40 <= t < 52):
            boxes.append(["scope_control_body", *_box(rng, 320 + t, 240, 120, 90)])
        if t % 5 != 0:
            boxes.append(["scope_mid_section", *_box(rng, 420 + 0.5 * t, 200 - t, 70, 40)])
        if t < 30:
            boxes.append(["scope_distal_end", *_box(rng, 520, 150, 30, 30)])
        if 10 <= t < 26:
            boxes.append(["syringe", *_box(rng, 500 - 4 * (t - 10), 160, 40, 20)])
        if 28 <= t < 42:
            for k in range(1 + t % 2):
                boxes.append(["air_gun", *_box(rng, 480 + 20 * k, 170 + t, 35, 25)])
        if 20 <= t < 35:
            boxes.append(["short_brush", *_box(rng, 300, 260, 50, 15)])
        if 35 <= t < 60 and t not in (44, 45, 46):
            boxes.append(["brush_tip_out", *_box(rng, 540 - 3 * (t - 35), 140 + t, 20, 20)])
        if 50 <= t < 58:
            boxes.append(["long_brush", *_box(rng, 400, 300, 90, 20)])
        if t % 7 == 0:
            boxes.append(["bubble", *_box(rng, 100, 100, 10, 10)])
        frames.append({"ts": round(t / FPS, 6), "boxes": boxes})
    return {"fps": FPS, "frame_width": WIDTH, "frame_height": HEIGHT, "frames": frames}


def _write_yolo_txt(data: dict, root: Path, with_conf: bool) -> list[Path]:
    paths = []
    for idx, frame in enumerate(data["frames"]):
        lines = []
        for name, x1, y1, x2, y2, conf in frame["boxes"]:
            cx, cy = (x1 + x2) / 2 / WIDTH, (y1 + y2) / 2 / HEIGHT
            w, h = (x2 - x1) / WIDTH, (y2 - y1) / HEIGHT
            cols = [CLASS_IDS[name], cx, cy, w, h] + ([conf] if with_conf else [])
            lines.append(" ".join(repr(c) for c in cols))
        path = root / f"{idx:06d}.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", required=True, type=Path)
    args = parser.parse_args()

    data = make_input()
    (HERE / "input.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "ref_features"
        pkg.mkdir()
        for name in ("clean_bbox_v2.py", "clean_bbox_v3.py", "nodep_concat.py"):
            shutil.copy(args.ref / name, pkg / name)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        sys.path.insert(0, tmp)
        nodep = importlib.import_module("ref_features.nodep_concat")

        expected = {}
        for key, with_conf in (("conf_default", False), ("conf_real", True)):
            label_dir = Path(tmp) / key
            label_dir.mkdir()
            feats, _names, version = nodep.build_nodep_concat_features(
                _write_yolo_txt(data, label_dir, with_conf),
                detection_mapping=MAPPING, fps=FPS, confidence_default=1.0,
            )
            assert version == "ama-v3-concat23-nodep-226d" and feats.shape == (FRAMES, 226)
            expected[key] = feats
    np.savez_compressed(HERE / "expected.npz", **expected)
    print(f"wrote {HERE / 'input.json'} and {HERE / 'expected.npz'}")


if __name__ == "__main__":
    main()
