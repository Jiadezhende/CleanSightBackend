"""CLEAN 历史离线策略 CleanMSTCNBiLSTMSegmenter：特征口径、分段解码、物料清单（合成数据 + 前向打桩，不加载真实权重）。

与旧 impl/clean.py 的逐位一致是迁移时的一次性核对（docs/update/20261017_OFFLINE_LEGACY_V2_PACKAGES.md）。
"""

import math

import numpy as np
import pytest

from factories import make_det_box, make_frame_detection

from app.types.detection import DetectorOutput
from app.services.inference.offline.segmentation.impl.clean_mstcn_bilstm.contract import LABELS
from app.services.inference.offline.segmentation.impl.clean_mstcn_bilstm.segmenter import CleanMSTCNBiLSTMSegmenter


def _frames(ts_list):
    """每帧 clean_large=[hand, scope_control_body]、clean_small=[short_brush]。"""
    out = []
    for ts in ts_list:
        large = DetectorOutput(boxes=[make_det_box(class_name="hand", bbox=[10, 10, 60, 80]),
                                      make_det_box(class_name="scope_control_body", bbox=[100, 100, 200, 220])],
                               metadata={}, timestamp=ts)
        small = DetectorOutput(boxes=[make_det_box(class_name="short_brush", bbox=[50, 50, 70, 90])],
                               metadata={}, timestamp=ts)
        out.append(make_frame_detection(ts=ts, by_source={"clean_large": large, "clean_small": small}))
    return out


def _make_label_probs(frame_count, label, conf):
    probs = np.full((frame_count, len(LABELS)), (1.0 - conf) / (len(LABELS) - 1), dtype=np.float32)
    probs[:, label] = conf
    return probs


def test_features_follow_contract(offline_context):
    mi = CleanMSTCNBiLSTMSegmenter(model_dir="m")._build_model_input(_frames([0.1, 0.2, 0.3, 0.4]), offline_context)
    assert (mi.frame_count, mi.feature_dim, mi.feature_version) == (4, 113, "clean_bbox_v2_top1_impute")
    assert all(math.isfinite(v) for row in mi.features for v in row)


def test_segment_decodes_runs_and_label_probs(monkeypatch, offline_context):
    seg = CleanMSTCNBiLSTMSegmenter(model_dir="m", min_duration_s=0.1)
    label = LABELS.index("short_brush_cleaning")
    monkeypatch.setattr(seg, "_predict_label_probs", lambda mi: _make_label_probs(mi.frame_count, label, 0.9))
    segs, probs = seg.infer(_frames([0.1, 0.2, 0.3]), offline_context)
    assert [(s.producer, s.label, s.start, s.end) for s in segs] == [("CleanMSTCNBiLSTMSegmenter", "short_brush_cleaning", 0.1, 0.3)]
    assert probs.labels == LABELS and probs.ts.tolist() == [0.1, 0.2, 0.3]
    assert probs.probs[:, label].tolist() == pytest.approx([0.9] * 3)


def test_segment_shorter_than_min_duration_dropped(monkeypatch, offline_context):
    seg = CleanMSTCNBiLSTMSegmenter(model_dir="m", min_duration_s=1.0)
    monkeypatch.setattr(seg, "_predict_label_probs", lambda mi: _make_label_probs(mi.frame_count, 1, 0.9))
    segments, _ = seg.infer(_frames([0.1, 0.2, 0.3]), offline_context)
    assert segments == []


def test_infer_rejects_missing_weights(tmp_path, offline_context):
    seg = CleanMSTCNBiLSTMSegmenter(model_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="model.pt"):
        seg.infer(_frames([0.1, 0.2]), offline_context)


def test_materials_is_model_file():
    assert [p.as_posix() for p in CleanMSTCNBiLSTMSegmenter(model_dir="m").materials()] == ["m/model.pt"]
