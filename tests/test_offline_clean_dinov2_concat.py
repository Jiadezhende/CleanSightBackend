"""857 维 DINOv2 策略的特征、缓存、物料和解码契约。"""

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from factories import make_det_box, make_detector_output, make_frame_detection

from app.types.frame import Frame
from app.services.inference.offline.segmentation.segmenter import OfflineContext
from app.services.inference.offline.segmentation.impl.clean_dinov2_concat import segmenter as seg_mod
from app.services.inference.offline.segmentation.impl.clean_dinov2_concat.contract import (
    ACTION_NAMES, EMBED_DIM, FEATURE_DIM, GEOMETRY_COLUMNS,
)
from app.services.inference.offline.segmentation.impl.clean_dinov2_concat.features import build_features


def _frame(ts, *boxes):
    output = make_detector_output(n=0, ts=ts)
    output.boxes = [make_det_box(class_name="hand", bbox=box) for box in boxes]
    return make_frame_detection(
        ts=ts, by_source={"clean_large": output}, frame_width=640, frame_height=480,
    )


def _unexpected(*args, **kwargs):
    pytest.fail("不应读取 raw 或访问嵌入缓存")


@pytest.fixture
def segmenter(tmp_path):
    model_dir = tmp_path / "model"
    backbone_dir = tmp_path / "backbone"
    model_dir.mkdir()
    backbone_dir.mkdir()
    strategy = seg_mod.CleanDINOv2ConcatSegmenter(str(model_dir), str(backbone_dir), device="cpu")
    for material in strategy.materials():
        material.touch()
    return strategy


def test_training_feature_layout_and_hand_means():
    rows = [[(0, 0.45, 0.5, 0.1, 0.1), (0, 0.65, 0.5, 0.1, 0.1)], [], [(0, 0.5, 0.5, 0.1, 0.1)]]
    embeddings = np.broadcast_to(np.array([[2], [6], [9]], dtype=np.float16), (3, EMBED_DIM))
    features = build_features(rows, [2, 0, 1], embeddings)
    assert len(GEOMETRY_COLUMNS) == 89
    assert features.shape == (3, FEATURE_DIM)
    assert features.dtype == np.float32
    np.testing.assert_array_equal(features[:, 0], np.array([2 / 3, 0, 1 / 3], dtype=np.float32))
    np.testing.assert_array_equal(features[:, 89:], np.broadcast_to([[4], [0], [9]], (3, EMBED_DIM)))


def test_handless_frames_need_no_raw_or_backbone(segmenter):
    context = OfflineContext(_unexpected, _unexpected, _unexpected)
    segmenter.materials()[1].unlink()
    segmenter.materials()[2].unlink()
    result = segmenter.preprocess([_frame(2.0)], context)
    np.testing.assert_array_equal(result.ts, [2.0])
    np.testing.assert_array_equal(result.features[0, 89:], np.zeros(EMBED_DIM, dtype=np.float32))


def test_missing_checkpoint_fails_before_raw_or_model_import(segmenter, monkeypatch):
    segmenter.materials()[0].unlink()
    monkeypatch.setitem(sys.modules, f"{seg_mod.__package__}.model", None)
    with pytest.raises(FileNotFoundError, match="model.pt"):
        segmenter.preprocess(
            [_frame(1.0, [100, 100, 200, 200])], OfflineContext(_unexpected, _unexpected, _unexpected),
        )
    with pytest.raises(FileNotFoundError, match="model.pt"):
        segmenter.segment(seg_mod.DINOv2ConcatInput(np.array([1.0]), np.zeros((1, FEATURE_DIM))))


@pytest.fixture
def raw_context(monkeypatch):
    store, calls = {}, []

    def video(**kwargs):
        calls.append(kwargs)
        for ts, value in ((1.0, 10), (2.0, 20), (3.0, 30)):
            yield Frame(timestamp=ts, frame=np.full((480, 640, 3), value, np.uint8))

    def embed(net, crops, device, batch_size, max_crops_per_s):
        return np.asarray([np.full(EMBED_DIM, crop[112, 112, 0]) for crop in crops], dtype=np.float16)

    monkeypatch.setattr(seg_mod, "load_dino", lambda *_: None)
    monkeypatch.setattr(seg_mod, "embed_crops", embed)
    monkeypatch.setattr(seg_mod.CleanDINOv2ConcatSegmenter, "_resolve_device", lambda self: "cpu")
    return OfflineContext(video, store.get, store.__setitem__), store, calls


@pytest.mark.parametrize("stale_key", ["ts", "boxes", "backbone"])
def test_cache_reuse_and_invalidation(segmenter, raw_context, stale_key):
    context, store, calls = raw_context
    frames = [_frame(1.0, [100, 100, 200, 200]), _frame(2.0), _frame(3.0, [100, 100, 200, 200])]
    first = segmenter.preprocess(frames, context)
    second = segmenter.preprocess(frames, context)
    np.testing.assert_array_equal(first.features[:, 89], [10, 0, 30])
    np.testing.assert_array_equal(first.features, second.features)
    assert calls == [{"width": 640, "height": 480, "start_ts": 1.0, "end_ts": 3.0}]
    assert set(store) == {seg_mod.CACHE_NAME}

    cached = store[seg_mod.CACHE_NAME]
    original = cached[stale_key].copy()
    cached[stale_key] = np.asarray("different") if stale_key == "backbone" else original + 1
    third = segmenter.preprocess(frames, context)
    assert len(calls) == 2
    np.testing.assert_array_equal(store[seg_mod.CACHE_NAME][stale_key], original)
    np.testing.assert_array_equal(third.features, first.features)


def test_missing_raw_frame_fails_without_caching_partial_embeddings(segmenter, raw_context):
    context, store, _ = raw_context
    frames = [_frame(1.0, [100, 100, 200, 200]), _frame(4.0, [100, 100, 200, 200])]
    with pytest.raises(ValueError, match="raw 录像缺 1/2"):
        segmenter.preprocess(frames, context)
    assert store == {}


def test_decode_uses_pp5_and_keeps_raw_five_class_probabilities(segmenter, monkeypatch):
    raw = np.array([0] * 5 + [2] * 6 + [3] * 4 + [2] * 6 + [0] * 5)
    probs = np.eye(len(ACTION_NAMES), dtype=np.float32)[raw]
    monkeypatch.setitem(
        sys.modules,
        f"{seg_mod.__package__}.model",
        SimpleNamespace(load_model=lambda path: None, predict_probs=lambda model, features: probs),
    )
    ts = np.arange(len(raw)) / 15
    segments = segmenter.segment(seg_mod.DINOv2ConcatInput(ts, np.zeros((len(raw), FEATURE_DIM))))
    assert [(s.label, s.start, s.end, s.producer) for s in segments] == [
        ("long_brush_insert", ts[5], ts[20], "CleanDINOv2ConcatSegmenter"),
    ]
    assert segmenter.label_probs().labels == ACTION_NAMES
    np.testing.assert_array_equal(segmenter.label_probs().probs, probs)


def test_shipped_yaml_selects_dinov2_concat_for_clean():
    from app.services.inference.config import load_stage_config
    from app.services.inference.stage_factory import StageFactory
    from app.services.inference.offline.compliance.config import parse_config

    config = load_stage_config()
    seg = StageFactory(config).create_offline_segmenter("2")
    assert isinstance(seg, seg_mod.CleanDINOv2ConcatSegmenter) and seg.uses_context
    assert (seg.model_dir.name, seg.backbone_dir.name) == ("clean-offline-dinov2-concat", "dinov2_vits14")
    assert (seg.min_segment_frames, seg.max_duration_s) == (5, 900.0)
    compliance = parse_config(config.get_stage_config("2").compliance)
    assert {label for labels in compliance.groups.values() for label in labels} <= set(seg.labels)
