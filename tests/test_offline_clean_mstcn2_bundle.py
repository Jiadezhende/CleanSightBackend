"""CLEAN 历史离线策略 CleanMSTCN2BundleSegmenter：94 维列序、帧率告警、pp20 分段（前向打桩，不加载真实权重）。

特征与训练侧 ama-v5-bundle-94d 逐位一致是接入时的一次性核对（docs/update/20261017_OFFLINE_LEGACY_REST_PACKAGES.md）。
"""

import numpy as np
import pytest

from factories import make_det_box, make_detector_output, make_frame_detection

from app.services.inference.offline.segmentation.impl.clean_mstcn2_bundle import segmenter as seg_mod
from app.services.inference.offline.segmentation.impl.clean_mstcn2_bundle.contract import ACTION_NAMES, FEATURE_COLUMNS
from app.services.inference.offline.segmentation.impl.clean_mstcn2_bundle.segmenter import CleanMSTCN2BundleSegmenter


def _frame(ts, *boxes):
    out = make_detector_output(n=0, ts=ts)
    out.boxes = list(boxes)
    return make_frame_detection(ts=ts, by_source={"clean_large": out}, frame_width=640, frame_height=480)


def _frames(n, fps=15.0):
    return [_frame(t / fps, make_det_box(class_name="hand", bbox=[10, 10, 60, 80]),
                   make_det_box(class_name="scope_control_body", bbox=[100, 100, 200, 220]),
                   make_det_box(class_name="scope_mid_section", bbox=[300, 200, 380, 260])) for t in range(n)]


def test_features_follow_training_column_order():
    mi = CleanMSTCN2BundleSegmenter(model_dir="m").preprocess(_frames(30))
    assert mi.features.shape == (30, 94) and len(FEATURE_COLUMNS) == 94
    assert np.isfinite(mi.features).all()


def test_pp20_merges_short_runs_and_skips_idle(monkeypatch, tmp_path):
    (tmp_path / "model.pt").write_bytes(b"x")
    labels = np.array([3] * 25 + [4] * 5 + [3] * 25 + [0] * 25)
    probs = np.full((labels.size, len(ACTION_NAMES)), 0.02, np.float32)
    probs[np.arange(labels.size), labels] = 0.9
    from app.services.inference.offline.segmentation.impl.clean_mstcn2_bundle import model as model_mod
    monkeypatch.setattr(model_mod, "load_model", lambda path: None)
    monkeypatch.setattr(model_mod, "predict_probs", lambda model, x: probs)
    seg = CleanMSTCN2BundleSegmenter(model_dir=str(tmp_path))
    segs = seg.segment(seg.preprocess(_frames(labels.size)))
    assert [(s.producer, s.label) for s in segs] == [("CleanMSTCN2BundleSegmenter", "long_brush_insert")]
    assert seg.label_probs().probs.shape == (80, 6)


def test_low_rate_only_warns(caplog):
    CleanMSTCN2BundleSegmenter(model_dir="m").preprocess(_frames(30, fps=10.0))
    assert "低于训练帧率" in caplog.text


def test_missing_weights_fail_before_loading_torch(tmp_path):
    seg = CleanMSTCN2BundleSegmenter(model_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="model.pt"):
        seg.segment(seg.preprocess(_frames(5)))


def test_materials_is_model_file():
    assert [p.as_posix() for p in CleanMSTCN2BundleSegmenter(model_dir="m").materials()] == ["m/model.pt"]
    assert seg_mod.FPS == 15.0
