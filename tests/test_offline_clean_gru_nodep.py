"""CLEAN 历史离线策略 CleanGRUNodepSegmenter：入模降采样、226 维特征、因果窗口、分段解码（前向打桩，不加载真实权重）。

与旧 impl/clean.py CleanNodepGRUSegmenter 的逐位一致是迁移时的一次性核对（docs/update/20261017_OFFLINE_LEGACY_REST_PACKAGES.md）。
"""

import math

import numpy as np
import pytest

from factories import make_det_box, make_frame_detection

from app.types.detection import DetectorOutput
from app.services.inference.offline.impl.clean_gru_nodep.contract import LABELS
from app.services.inference.offline.impl.clean_gru_nodep.model import causal_windows
from app.services.inference.offline.impl.clean_gru_nodep.segmenter import CleanGRUNodepSegmenter


def _frames(ts_list):
    out = []
    for ts in ts_list:
        large = DetectorOutput(boxes=[make_det_box(class_name="hand", bbox=[10, 10, 60, 80]),
                                      make_det_box(class_name="scope_control_body", bbox=[100, 100, 200, 220])],
                               metadata={}, timestamp=ts)
        small = DetectorOutput(boxes=[make_det_box(class_name="short_brush", bbox=[50, 50, 70, 90])],
                               metadata={}, timestamp=ts)
        out.append(make_frame_detection(ts=ts, by_source={"clean_large": large, "clean_small": small}))
    return out


def _segmenter(**kw):
    return CleanGRUNodepSegmenter(model_dir="m", model_input_fps=7.5, **kw)


def test_resamples_to_model_fps_then_builds_226d(monkeypatch):
    ts = [round(i / 15, 6) for i in range(8)]
    seg = _segmenter(min_duration_s=0.0)
    label = LABELS.index("short_brush_cleaning")

    def onehot(mi):
        probs = np.full((mi.frame_count, len(LABELS)), 0.02, dtype=np.float32)
        probs[:, label] = 0.9
        return probs

    monkeypatch.setattr(seg, "_predict_with_model", onehot)
    mi = seg.preprocess(_frames(ts))
    assert (mi.feature_dim, mi.feature_version, mi.timestamps) == (226, "ama-v3-concat23-nodep-226d", ts[::2])
    assert all(math.isfinite(v) for row in mi.features for v in row)
    segs = seg.segment(mi)
    assert [(s.producer, s.label, s.start, s.end) for s in segs] == [
        ("CleanGRUNodepSegmenter", "short_brush_cleaning", ts[0], ts[6]),
    ]
    assert seg.label_probs().labels == LABELS


def test_rejects_detection_rate_below_model_fps():
    with pytest.raises(ValueError, match="低于契约帧率"):
        _segmenter().preprocess(_frames([i / 5 for i in range(6)]))


def test_causal_windows_end_at_own_frame_and_pad_with_first():
    x = np.arange(10, dtype=np.float32).reshape(5, 2)
    w = causal_windows(x, 3)
    np.testing.assert_array_equal(w[:, -1], x)
    np.testing.assert_array_equal(w[1], x[[0, 0, 1]])


def test_missing_weights_fail_before_loading_torch(tmp_path):
    seg = CleanGRUNodepSegmenter(model_dir=str(tmp_path), model_input_fps=7.5)
    with pytest.raises(FileNotFoundError, match="model.pt"):
        seg.segment(seg.preprocess(_frames([i / 15 for i in range(4)])))


@pytest.mark.parametrize("kw", [{"model_input_fps": 0}, {"model_input_fps": 7.5, "confidence_override": 1.5}])
def test_bad_params_fail_at_construction(kw):
    with pytest.raises(ValueError):
        CleanGRUNodepSegmenter(model_dir="m", **kw)


def test_materials_is_model_file():
    assert [p.as_posix() for p in _segmenter().materials()] == ["m/model.pt"]
