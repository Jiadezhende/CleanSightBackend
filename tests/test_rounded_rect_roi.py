"""B2：_draw_rounded_rect ROI 局部化的行为等价性单测。

旧实现整帧 copy + 整帧 addWeighted；新实现只在矩形包围盒 ROI 上操作。二者像素级等价
（矩形外 overlay==frame，addWeighted 还原原像素），ROI 化是纯加速。
"""

import numpy as np

from app.services.inference.visualization.visualizer import FixedVisualizer


def _reference_full_frame(frame, pt1, pt2, color, radius, alpha):
    """改造前的整帧实现，作为等价性基准。"""
    import cv2

    x1, y1 = pt1
    x2, y2 = pt2
    overlay = frame.copy()
    cv2.rectangle(overlay, (x1 + radius, y1), (x2 - radius, y2), color, -1)
    cv2.rectangle(overlay, (x1, y1 + radius), (x2, y2 - radius), color, -1)
    for cx, cy in [(x1 + radius, y1 + radius), (x2 - radius, y1 + radius),
                   (x1 + radius, y2 - radius), (x2 - radius, y2 - radius)]:
        cv2.circle(overlay, (cx, cy), radius, color, -1)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, dst=frame)
    return frame


def _base_frame():
    rng = np.random.RandomState(0)
    return rng.randint(0, 255, (480, 640, 3), dtype=np.uint8)


def test_roi_matches_full_frame_reference():
    """ROI 版与整帧基准像素级一致。"""
    pt1, pt2, color, radius, alpha = (100, 80), (260, 140), (0, 0, 0), 8, 0.6

    roi_out = _base_frame()
    FixedVisualizer._draw_rounded_rect(roi_out, pt1, pt2, color, radius, alpha)

    ref_out = _reference_full_frame(_base_frame(), pt1, pt2, color, radius, alpha)

    assert np.array_equal(roi_out, ref_out)


def test_pixels_outside_rect_unchanged():
    """矩形包围盒外像素与原帧完全一致（证 ROI 化无观感外溢）。"""
    original = _base_frame()
    out = original.copy()
    pt1, pt2 = (200, 150), (400, 260)
    FixedVisualizer._draw_rounded_rect(out, pt1, pt2, (0, 0, 0), 8, 0.6)

    # 远离矩形的区域不应被触碰
    assert np.array_equal(out[:140, :], original[:140, :])      # 矩形上方
    assert np.array_equal(out[270:, :], original[270:, :])      # 矩形下方
    assert np.array_equal(out[:, :190], original[:, :190])      # 矩形左侧
    assert np.array_equal(out[:, 410:], original[:, 410:])      # 矩形右侧


def test_rect_partially_offscreen_is_clipped():
    """矩形越过帧边界时不抛异常，仅在交集 ROI 上绘制。"""
    out = _base_frame()
    # 左上角越界
    FixedVisualizer._draw_rounded_rect(out, (-30, -20), (60, 50), (255, 0, 0), 6, 0.7)
    assert out.shape == (480, 640, 3)


def test_fully_offscreen_is_noop():
    """完全在帧外的矩形 → 帧不变。"""
    original = _base_frame()
    out = original.copy()
    FixedVisualizer._draw_rounded_rect(out, (700, 500), (800, 560), (0, 0, 0), 6, 0.7)
    assert np.array_equal(out, original)
