"""离线 clean 特征工程的**规模守卫**：按帧装桶与旧稠密实现逐值等价，且内存线性于检测框数。

背景：`_collect_object_arrays` 曾给每个检测框分配一条全长 `[T,5]` 稀疏数组（填充率 1/T），
内存 `20 B × D × T` 即 O(T²)；选槽位又每帧重扫整段全部数组，时间同为 O(T²)。T=9000
（10 min step）约 8 GB / ≥12 min，跑不动。改成按帧装桶后两者都退回线性。
完整定位与决策见 docs/update/20260905_TEMPORAL_MEMORY_BOUNDS_PROPOSAL.md（D1）。

本文件守两件事：
1. **逐值等价** —— 装桶不是近似优化，输出必须与旧实现全等。文件内保留一份旧稠密实现作参考
   （唯一一处副本，刻意隔离在测试里，不留在生产代码）。
2. **内存形状** —— 桶占用严格等于 `20 B × 检测框数`。确定性断言，不靠计时、不会 flake。
"""

import numpy as np
import pytest

from app.domain.detection import Detection, FrameDetections, FrameFeature
from app.services.inference.offline.impl import clean as clean_mod
from app.services.inference.offline.impl.clean import (
    OBJECTS,
    _as_box5,
    _collect_object_buckets,
    _select_hand_slots,
    _select_top1_slot,
    build_base_features,
)

FRAME_W, FRAME_H = 640, 480


# ============================ 旧稠密实现（参考副本，仅供等价断言） ============================


def _collect_object_arrays_dense(frames, frame_width, frame_height):
    """`_collect_object_arrays` 在 HEAD~ 的原样形态：{obj: [每检测框一个 [T,5] 稀疏数组]}。"""
    frame_count = len(frames)
    out = {name: [] for name in OBJECTS}
    for idx, ff in enumerate(frames):
        width = max(1, int(ff.frame_width or frame_width))
        height = max(1, int(ff.frame_height or frame_height))
        for fd in ff.by_source.values():
            for det in fd.detections:
                obj = clean_mod.OBJECT_ALIASES.get(str(det.class_name))
                if obj is None:
                    continue
                cx, cy, area = clean_mod._bbox_to_center_area(det, width, height)
                arr = np.zeros((frame_count, 5), dtype=np.float32)
                arr[idx] = (1.0, float(cx), float(cy), float(area),
                            max(0.0, min(1.0, float(det.confidence))))
                out[obj].append(arr)
    return out


def _select_hand_slots_dense(hand_arrs, frames):
    hand_count = np.zeros(frames, dtype=np.float32)
    slots = [np.zeros((frames, 5), dtype=np.float32), np.zeros((frames, 5), dtype=np.float32)]
    for t in range(frames):
        candidates = [_as_box5(arr[t]) for arr in hand_arrs if _as_box5(arr[t])[0] > 0]
        hand_count[t] = len(candidates)
        candidates.sort(key=lambda row: clean_mod._box_score(row), reverse=True)
        for slot_idx, row in enumerate(candidates[:2]):
            slots[slot_idx][t] = row
    return hand_count, slots


def _select_top1_slot_dense(arrs, frames):
    count = np.zeros(frames, dtype=np.float32)
    slot = np.zeros((frames, 5), dtype=np.float32)
    prev_center = None
    for t in range(frames):
        candidates = [_as_box5(arr[t]) for arr in arrs if _as_box5(arr[t])[0] > 0]
        count[t] = len(candidates)
        if not candidates:
            continue
        candidates.sort(key=lambda row: clean_mod._box_score(row, prev_center), reverse=True)
        slot[t] = candidates[0]
        prev_center = slot[t, 1:3]
    return count, slot


# ============================ 合成输入 ============================


def _synth_frames(frame_count: int, boxes_per_frame: int, fps: float = 15.0, seed: int = 7):
    """合成 FrameFeature 序列：每帧 boxes_per_frame 个框，类别/坐标/置信度伪随机。

    与提案 §三 实测同口径（5 框/帧、15 fps）。刻意让同帧出现多个同类目标（尤其 hand），
    才能真正压到「帧内候选排序」这条等价路径；也刻意留缺帧（部分帧某类目标为空）压 impute。
    """
    rng = np.random.default_rng(seed)
    frames = []
    for i in range(frame_count):
        dets = []
        for _ in range(boxes_per_frame):
            # hand 权重更高：top-2 槽位竞争是唯一有「同帧多候选排序」的路径
            name = OBJECTS[int(rng.integers(0, len(OBJECTS)))] if rng.random() < 0.5 else "hand"
            x1 = float(rng.integers(0, FRAME_W - 40))
            y1 = float(rng.integers(0, FRAME_H - 40))
            w = float(rng.integers(8, 40))
            h = float(rng.integers(8, 40))
            dets.append(Detection(
                bbox=[x1, y1, x1 + w, y1 + h],
                confidence=float(rng.random()),
                class_id=0,
                class_name=name,
            ))
        ts = i / fps
        # 两流：多流按帧合并这条路径也要覆盖到
        frames.append(FrameFeature(
            ts=ts,
            by_source={
                "clean_large": FrameDetections(detections=dets[: len(dets) // 2], metadata={}, timestamp=ts),
                "clean_small": FrameDetections(detections=dets[len(dets) // 2:], metadata={}, timestamp=ts),
            },
            frame_width=FRAME_W,
            frame_height=FRAME_H,
        ))
    return frames


def _detection_count(frames) -> int:
    return sum(len(fd.detections) for ff in frames for fd in ff.by_source.values())


# ============================ 等价 ============================


@pytest.mark.parametrize("frame_count,boxes_per_frame", [(800, 5), (1500, 3)])
def test_bucket_equivalence_vs_dense_reference(frame_count, boxes_per_frame):
    """按帧装桶与旧稠密实现在全部 9 类目标上逐值全等（np.array_equal，不是 allclose）。

    等价的三条依据：桶内顺序 = 旧实现在该帧上的投影（同为检测遍历序）、`list.sort` 稳定、
    桶内 present 恒 1 使旧的 `[0] > 0` 过滤恒真。任一条被破坏，这个用例就会红。
    """
    frames = _synth_frames(frame_count, boxes_per_frame)
    buckets = _collect_object_buckets(frames, FRAME_W, FRAME_H)
    dense = _collect_object_arrays_dense(frames, FRAME_W, FRAME_H)

    # hand：top-2 槽位 + 逐帧计数
    hand_count, hand_slots = _select_hand_slots(buckets["hand"], frame_count)
    ref_count, ref_slots = _select_hand_slots_dense(dense["hand"], frame_count)
    assert np.array_equal(hand_count, ref_count), "hand_count 与旧实现不等"
    for slot_idx, (got, want) in enumerate(zip(hand_slots, ref_slots)):
        assert np.array_equal(got, want), f"hand top{slot_idx + 1} 槽位与旧实现不等"

    # 其余 8 类：top-1 槽位 + 候选计数
    for obj in OBJECTS:
        if obj == "hand":
            continue
        count, slot = _select_top1_slot(buckets[obj], frame_count)
        ref_c, ref_s = _select_top1_slot_dense(dense[obj], frame_count)
        assert np.array_equal(count, ref_c), f"{obj} candidate_count 与旧实现不等"
        assert np.array_equal(slot, ref_s), f"{obj} top1 槽位与旧实现不等"


def test_full_feature_matrix_equivalence():
    """整条 build_base_features 的 113 维矩阵与「旧收集 + 旧选槽位」逐值全等。

    上一个用例守收集/选槽位这一段；这个守到出口，确保 `_build_feature_matrix` 的接线
    （impute / 目标对 / 时间编码）没在换数据结构时被改动。
    """
    frame_count = 600
    frames = _synth_frames(frame_count, 5, seed=13)
    got = build_base_features(frames, fps=15.0, frame_width=FRAME_W, frame_height=FRAME_H)

    # 用旧稠密结构走同一条 _build_feature_matrix：临时把两个选槽位函数换回旧实现
    dense = _collect_object_arrays_dense(frames, FRAME_W, FRAME_H)
    effective_fps = clean_mod._effective_fps([ff.ts for ff in frames], 15.0)
    orig_hand, orig_top1 = clean_mod._select_hand_slots, clean_mod._select_top1_slot
    try:
        clean_mod._select_hand_slots = _select_hand_slots_dense
        clean_mod._select_top1_slot = _select_top1_slot_dense
        want, want_names = clean_mod._build_feature_matrix(dense, frame_count, effective_fps)
    finally:
        clean_mod._select_hand_slots, clean_mod._select_top1_slot = orig_hand, orig_top1

    assert got.feature_names == want_names
    assert np.array_equal(got.features, want)


# ============================ 内存形状 ============================


def test_collect_buckets_memory_is_linear():
    """桶占用严格 = 20 B × 检测框数（旧实现是 20 B × D × T，T=4000 时约 1.6 GB）。"""
    frame_count, boxes_per_frame = 4000, 5
    frames = _synth_frames(frame_count, boxes_per_frame)
    buckets = _collect_object_buckets(frames, FRAME_W, FRAME_H)

    rows = [row for per_frame in buckets.values() for bucket in per_frame for row in bucket]
    total_bytes = sum(row.nbytes for row in rows)
    # 每帧的框可能落在 OBJECT_ALIASES 之外（本合成里不会），故按实际入桶行数核对
    assert len(rows) == _detection_count(frames)
    assert total_bytes == 20 * len(rows), (
        f"桶占用 {total_bytes} B ≠ 20 B × {len(rows)} 框：时间轴又被物化了（O(T²) 回归）"
    )
    # 与旧实现的量级对照：同输入下旧实现是 20 × D × T ≈ 1.6 GB
    assert total_bytes < 1 * 1024 * 1024


def test_build_base_features_ndarray_contract():
    """长序列跑通 build_base_features，并钉住 ModelInput.features 的 ndarray 契约（D6）。"""
    frame_count = 4000
    frames = _synth_frames(frame_count, 5)
    mi = build_base_features(frames, fps=15.0, frame_width=FRAME_W, frame_height=FRAME_H)

    assert isinstance(mi.features, np.ndarray)
    assert mi.features.dtype == np.float32
    assert mi.features.shape == (frame_count, 113)
    assert mi.frame_count == frame_count and mi.feature_dim == 113
    assert np.isfinite(mi.features).all()


def test_empty_input_keeps_ndarray_shape():
    """空输入也返 [0,113] ndarray（不是 []），下游 shape 推导不用为空分支特判。"""
    mi = build_base_features([], fps=15.0)
    assert isinstance(mi.features, np.ndarray)
    assert mi.features.shape == (0, 113)
    assert mi.frame_count == 0 and mi.feature_dim == 113
