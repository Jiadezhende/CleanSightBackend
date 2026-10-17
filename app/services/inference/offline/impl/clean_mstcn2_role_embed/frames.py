"""帧级检测 → 训练口径的逐帧检测行 ``[(类别号, cx, cy, w, h)]``。

    rows = detection_rows(frames, width=640, height=480)

训练输入是 auto-annotate 产出的逐帧框文件（15 fps，坐标归一化、6 位小数、不带置信度列）。这里按同一口径从
``FrameDetection`` 还原：每帧每类按框面积降序保留前 K 个（hand 2 个、其余 1 个），行按类别号升序、类内面积降序。
"""

from __future__ import annotations

from typing import Sequence

from app.types.detection import FrameDetection

FPS = 15.0
CLASS_NAMES = {0: "hand", 1: "scope_control_body", 2: "scope_mid_section", 3: "scope_distal_end",
               4: "syringe", 5: "air_gun", 6: "short_brush", 7: "brush_tip_out"}
HAND, CTRL, MID = 0, 1, 2
CLASS_IDS = {name: cid for cid, name in CLASS_NAMES.items()}
TOP_K = {HAND: 2}  # 其余类 1 个


def detection_rows(frames: Sequence[FrameDetection], *, width: int, height: int) -> list[list[tuple]]:
    """逐帧检测行；``class_name`` 不在 ``CLASS_NAMES`` 里的框忽略（与训练映射一致）。"""
    out = []
    for frame in frames:
        by_class: dict[int, list[tuple]] = {}
        for output in frame.by_source.values():
            for det in output.boxes:
                cid = CLASS_IDS.get(det.class_name)
                if cid is None:
                    continue
                x1, y1, x2, y2 = (float(v) for v in det.bbox)
                w, h = (x2 - x1) / width, (y2 - y1) / height
                by_class.setdefault(cid, []).append(((x1 + x2) / 2 / width, (y1 + y2) / 2 / height, w, h))
        rows = []
        for cid in sorted(by_class):
            boxes = sorted(by_class[cid], key=lambda b: b[2] * b[3], reverse=True)[:TOP_K.get(cid, 1)]
            rows += [(cid, *(round(v, 6) for v in box)) for box in boxes]
        out.append(rows)
    return out
