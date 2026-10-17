"""CLEAN C7 离线策略：检测 → 训练口径检测行、取帧裁剪的对齐与缺帧、分段解码、配置装配。

特征数值与训练逐位一致是接入时的一次性核对（见 docs/update/20261017_OFFLINE_CLEAN_C7.md），不在这里重放；
这里不加载任何真实权重、不跑 torch 前向。
"""

import numpy as np
import pytest

from factories import make_det_box, make_detector_output, make_frame_detection

from app.types.frame import Frame
from app.services.inference.offline.segmenter import OfflineContext
from app.services.inference.offline.impl.clean_c7 import segmenter as seg_mod
from app.services.inference.offline.impl.clean_c7.contract import ACTION_NAMES
from app.services.inference.offline.impl.clean_c7.frames import detection_rows
from app.services.inference.offline.impl.clean_c7.segmenter import CleanC7Segmenter


def _det(name, bbox):
    return make_det_box(class_name=name, bbox=bbox)


def _frame(ts, *boxes, w=640, h=480):
    out = make_detector_output(n=0, ts=ts)
    out.boxes = list(boxes)
    return make_frame_detection(ts=ts, by_source={"clean_large": out}, frame_width=w, frame_height=h)


def _segmenter(**kw):
    return CleanC7Segmenter(model_dir="m", backbone_dir="b", **kw)


class TestDetectionRows:
    def test_area_top_k_class_order_normalized(self):
        frame = _frame(
            1.0,
            _det("syringe", [0, 0, 64, 48]),
            _det("hand", [0, 0, 10, 10]),           # 最小的手，top-2 外
            _det("hand", [100, 100, 164, 196]),     # 面积第一
            _det("syringe", [0, 0, 128, 96]),       # 面积更大的 syringe 胜出
            _det("hand", [0, 0, 32, 48]),
            _det("bubble", [0, 0, 600, 400]),       # 不在 C7 类表里 → 忽略
        )
        rows = detection_rows([frame], width=640, height=480)[0]
        assert rows == [
            (0, 0.20625, 0.308333, 0.1, 0.2),
            (0, 0.025, 0.05, 0.05, 0.1),
            (4, 0.1, 0.1, 0.2, 0.2),
        ]

    def test_empty_frame_keeps_its_slot(self):
        assert detection_rows([_frame(1.0), _frame(2.0, _det("hand", [0, 0, 64, 48]))],
                              width=640, height=480)[0] == []


def _video(frames_by_ts, calls=None):
    def video(**kw):
        if calls is not None:
            calls.append(kw)
        for ts, pixel in frames_by_ts:
            yield Frame(timestamp=ts, frame=np.full((480, 640, 3), pixel, np.uint8))
    return video


class TestEmbed:
    @pytest.fixture(autouse=True)
    def _stub_backbone(self, monkeypatch):
        """DINOv2 打桩：嵌入 = 裁剪图中心像素值（能看出每行来自哪一帧）。"""
        monkeypatch.setattr(seg_mod, "load_dino", lambda backbone_dir, device: None)
        monkeypatch.setattr(CleanC7Segmenter, "_resolve_device", lambda self: "cpu")

        def embed(net, crops, device, batch_size, max_crops_per_s):
            rows = [np.full(768, crop[112, 112, 0], np.float16) for crop in crops]
            return np.stack(rows) if rows else np.zeros((0, 768), np.float16)

        monkeypatch.setattr(seg_mod, "embed_crops", embed)

    def test_rows_follow_frame_then_box_order_and_skip_handless_frames(self):
        ts = np.array([1.0, 2.0, 3.0])
        hands = [[(0.5, 0.5, 0.1, 0.1), (0.6, 0.6, 0.1, 0.1)], [], [(0.5, 0.5, 0.1, 0.1)]]
        calls = []
        emb = _segmenter()._embed(ts, hands, _video([(0.5, 9), (1.0, 10), (2.0, 20), (3.0, 30)], calls))
        assert emb[:, 0].tolist() == [10, 10, 30]
        assert calls == [{"width": 640, "height": 480, "start_ts": 1.0, "end_ts": 3.0}]

    def test_missing_frame_fails_instead_of_skipping_hands(self):
        hands = [[(0.5, 0.5, 0.1, 0.1)], [(0.5, 0.5, 0.1, 0.1)]]
        with pytest.raises(ValueError, match="缺 1/2"):
            _segmenter()._embed(np.array([1.0, 2.0]), hands, _video([(1.0, 10)]))

    def test_no_hands_never_touches_video_or_backbone(self):
        emb = _segmenter()._embed(np.array([1.0]), [[]], _video([], calls := []))
        assert emb.shape == (0, 768) and calls == []


class TestSegments:
    def test_idle_skipped_conf_is_label_mean(self):
        labels = np.array([0, 3, 3, 3, 0, 4, 4])
        probs = np.zeros((7, len(ACTION_NAMES)), np.float32)
        probs[np.arange(7), labels] = [1, 0.5, 0.7, 0.9, 1, 0.6, 0.8]
        ts = np.arange(7, dtype=np.float64) / 15
        segs = _segmenter()._segments(ts, labels, probs)
        assert [(s.label, s.start, s.end) for s in segs] == [
            ("long_brush_insert", ts[1], ts[3]),
            ("long_brush_withdraw", ts[5], ts[6]),
        ]
        assert [round(s.conf, 6) for s in segs] == [0.7, 0.7]
        assert {s.producer for s in segs} == {"CleanC7Segmenter"}


class TestContract:
    def test_rejects_non_640x480(self):
        frames = [_frame(t / 15, w=1280, h=720) for t in range(3)]
        with pytest.raises(ValueError, match="640×480"):
            _segmenter().preprocess(frames, OfflineContext(video=_video([])))

    def test_rejects_detection_rate_below_15fps(self):
        frames = [_frame(t / 7.5) for t in range(5)]
        with pytest.raises(ValueError, match="低于契约帧率"):
            _segmenter().preprocess(frames, OfflineContext(video=_video([])))

    @pytest.mark.parametrize("kw", [
        {"device": "mps"}, {"max_crops_per_s": 0}, {"batch_size": 0}, {"min_segment_frames": -1},
    ])
    def test_bad_params_fail_at_construction(self, kw):
        with pytest.raises(ValueError):
            _segmenter(**kw)

    def test_shipped_yaml_builds_c7(self):
        from app.services.inference.config import load_stage_config
        from app.services.inference.stage_factory import StageFactory

        seg = StageFactory(load_stage_config()).create_offline_segmenter("2")
        assert isinstance(seg, CleanC7Segmenter) and seg.uses_context
        assert (seg.model_dir.name, seg.backbone_dir.name) == ("clean-offline-c7-v41", "dinov2_vits14")
