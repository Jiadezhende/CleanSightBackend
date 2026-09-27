"""CleanNodepGRUSegmenter：nodep-226d 特征与训练框架逐位对齐 + 滑窗 GRU 加载 / 推理契约。"""

import json
from pathlib import Path

import numpy as np
import pytest
from factories import make_det_box

from app.domain.detection import DetectorOutput, FrameDetection
from app.services.inference.offline.impl.clean import (
    NODEP_FEATURE_DIM,
    NODEP_FEATURE_VERSION,
    NODEP_GRU_LABELS,
    CleanNodepGRUSegmenter,
    _causal_windows,
    build_nodep_concat_features,
)

GOLDEN = Path(__file__).parent / "fixtures" / "clean_nodep_golden"
_LARGE = {"hand", "scope_control_body", "scope_mid_section"}


def _golden_frames():
    """input.json → FrameDetection（按在线分组拆 clean_large / clean_small 两流，验证多流按帧合并）。"""
    data = json.loads((GOLDEN / "input.json").read_text(encoding="utf-8"))
    frames = []
    for frame in data["frames"]:
        large, small = [], []
        for name, x1, y1, x2, y2, conf in frame["boxes"]:
            box = make_det_box(bbox=[x1, y1, x2, y2], confidence=conf, class_name=name)
            (large if name in _LARGE else small).append(box)
        by_source = {
            "clean_large": DetectorOutput(boxes=large, metadata={}, timestamp=frame["ts"]),
            "clean_small": DetectorOutput(boxes=small, metadata={}, timestamp=frame["ts"]),
        }
        frames.append(FrameDetection(
            ts=frame["ts"], by_source=by_source,
            frame_width=data["frame_width"], frame_height=data["frame_height"],
        ))
    return data, frames


class TestNodepFeatureParity:
    @pytest.mark.parametrize("override, key", [(1.0, "conf_default"), (None, "conf_real")])
    def test_matches_training_framework(self, override, key):
        data, frames = _golden_frames()
        expected = np.load(GOLDEN / "expected.npz")[key]
        mi = build_nodep_concat_features(frames, data["fps"], confidence_override=override)
        assert mi.feature_version == NODEP_FEATURE_VERSION
        assert mi.feature_dim == NODEP_FEATURE_DIM
        assert mi.timestamps == [f["ts"] for f in data["frames"]]
        np.testing.assert_allclose(np.asarray(mi.features, dtype=np.float32), expected, atol=1e-5, rtol=0)

    def test_deprecated_objects_read_as_never_present(self):
        """废弃类检测被丢弃：其块等同「全程未检出」——只有 missing_age 饱和为 1，其余通道全零。"""
        data, frames = _golden_frames()
        mi = build_nodep_concat_features(frames, data["fps"], confidence_override=1.0)
        x = np.asarray(mi.features)
        for i, name in enumerate(mi.feature_names):
            col = name.split(".", 1)[1]
            if not col.startswith(("short_brush_", "long_brush_", "scope_distal_end_")):
                continue
            if col.endswith("_missing_age"):
                assert x[-1, i] == 1.0, name
            else:
                assert not x[:, i].any(), name

    def test_empty_input(self):
        mi = build_nodep_concat_features([], 7.5)
        assert mi.frame_count == 0 and mi.feature_version == NODEP_FEATURE_VERSION


class TestCausalWindows:
    def test_each_window_ends_at_its_frame_and_head_repeats_first(self):
        x = np.arange(10, dtype=np.float32).reshape(5, 2)
        w = _causal_windows(x, 3)
        assert w.shape == (5, 3, 2)
        np.testing.assert_array_equal(w[:, -1], x)
        np.testing.assert_array_equal(w[0], np.repeat(x[:1], 3, axis=0))
        np.testing.assert_array_equal(w[1], x[[0, 0, 1]])
        np.testing.assert_array_equal(w[4], x[2:5])


class TestSegmenterContract:
    def test_model_input_fps_required_and_positive(self):
        with pytest.raises(TypeError):
            CleanNodepGRUSegmenter(model_path="x.pt")
        with pytest.raises(ValueError, match="model_input_fps"):
            CleanNodepGRUSegmenter(model_path="x.pt", model_input_fps=0)
        with pytest.raises(ValueError, match="confidence_override"):
            CleanNodepGRUSegmenter(model_path="x.pt", model_input_fps=7.5, confidence_override=2.0)

    def test_preprocess_downsamples_to_model_fps_keeping_real_ts(self):
        data, frames = _golden_frames()  # 7.5fps 原始序列 → 当作 15fps 检测，降到 3.75fps
        seg = CleanNodepGRUSegmenter(model_input_fps=3.75)
        mi = seg.preprocess(frames)
        assert 0 < mi.frame_count < len(frames)
        assert set(mi.timestamps) <= {f.ts for f in frames}
        assert mi.fps == 3.75


# ============================ 加载 / 推理（小权重，需 torch） ============================


def _write_artifact(tmp_path, *, num_classes=len(NODEP_GRU_LABELS)):
    """模拟训练框架 checkpoint 单文件（training_state，权重在 model_state）。"""
    torch = pytest.importorskip("torch")
    from app.services.inference.offline.impl.clean import _make_window_gru

    torch.manual_seed(0)
    model = _make_window_gru(NODEP_FEATURE_DIM, num_classes)
    path = tmp_path / "gru.pt"
    torch.save({"schema_version": 1, "checkpoint_kind": "training_state", "model_state": model.state_dict(),
                "optimizer_state": {}}, path)
    return path


class TestSegmenterWithModel:
    def test_segment_emits_six_class_probs_on_resampled_ts(self, tmp_path):
        path = _write_artifact(tmp_path)
        data, frames = _golden_frames()
        seg = CleanNodepGRUSegmenter(model_path=str(path), model_input_fps=data["fps"], min_duration_s=0.0)
        mi = seg.preprocess(frames)
        segs = seg.segment(mi)

        probs = seg.label_probs()
        assert probs.labels == NODEP_GRU_LABELS
        assert probs.probs.shape == (mi.frame_count, len(NODEP_GRU_LABELS))
        assert probs.ts.tolist() == mi.timestamps
        np.testing.assert_allclose(probs.probs.sum(axis=1), 1.0, atol=1e-5)
        assert all(s.producer == "CleanNodepGRUSegmenter" and s.label in NODEP_GRU_LABELS[1:] for s in segs)

    def test_prediction_is_causal_within_feature_matrix(self, tmp_path):
        """滑窗只看过去：同一特征矩阵上，第 t 帧概率与 t 之后的帧无关。"""
        path = _write_artifact(tmp_path)
        data, frames = _golden_frames()
        seg = CleanNodepGRUSegmenter(model_path=str(path), model_input_fps=data["fps"])
        mi = seg.preprocess(frames)
        full = seg._predict_with_model(mi)
        head = type(mi)(features=mi.features[:20], feature_names=mi.feature_names,
                        timestamps=mi.timestamps[:20], fps=mi.fps, feature_version=mi.feature_version)
        np.testing.assert_allclose(seg._predict_with_model(head), full[:20], atol=1e-6)

    def test_class_count_mismatch_fails(self, tmp_path):
        path = _write_artifact(tmp_path, num_classes=5)
        data, frames = _golden_frames()
        seg = CleanNodepGRUSegmenter(model_path=str(path), model_input_fps=data["fps"])
        with pytest.raises(RuntimeError, match="size mismatch"):
            seg.segment(seg.preprocess(frames))

    def test_missing_weight_fails(self, tmp_path):
        data, frames = _golden_frames()
        seg = CleanNodepGRUSegmenter(model_path=str(tmp_path / "absent.pt"), model_input_fps=data["fps"])
        with pytest.raises(FileNotFoundError):
            seg.segment(seg.preprocess(frames))
