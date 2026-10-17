"""impl/clean.py 里的历史 CLEAN 策略（MS-TCN+BiLSTM / ASFormer / BiGRU / nodep GRU）。

这些策略正在逐个迁成自包含包；本文件随 clean.py 一起删除。框架行为的用例在 test_offline_pipeline.py（只用替身）。
"""

import math

import pytest

from factories import make_det_box, make_frame_detection, make_run

from app.types.detection import DetectorOutput
from app.services.inference.config import InferenceConfig
from app.services.inference.offline.runner import OfflineRunner, OfflineRunSpec
from app.storage import inference as inference_store

_CLEAN_CLASS = "app.services.inference.offline.impl.clean.CleanMSTCNBiLSTMSegmenter"


def _frames(per_source):
    """{src: [DetectorOutput 按 ts]} → List[FrameDetection]（按 ts 对齐+升序），供直调 preprocess。"""
    by_ts: dict = {}
    for src, fds in per_source.items():
        for fd in fds:
            by_ts.setdefault(fd.timestamp, {})[src] = fd
    return [make_frame_detection(ts=ts, by_source=by_ts[ts]) for ts in sorted(by_ts)]


def _config(offline):
    return InferenceConfig({"stages": {"2": {
        "detectors": [{"name": "clean_large"}, {"name": "clean_small"}],
        "offline": offline,
    }}})


def _runner(offline):
    return OfflineRunner(config=_config(offline))


def _inference_dir(task_id, step_id):
    from app.storage.utils import root as _root

    return _root.run_path(make_run(task_id, step_id), "inference")


def _facts_path(root, task_id=1, step_id=2):
    return _inference_dir(task_id, step_id) / "temporal.jsonl"


def _probs_path(root, task_id=1, step_id=2):
    return _inference_dir(task_id, step_id) / "label_probs.npz"


def _onehot_probs(frame_count, label, conf, class_count=None):
    """打桩用逐帧 softmax：目标列 conf，其余列均分余量。class_count 缺省为旧三模型的 ACTION_LABELS 类数。"""
    import numpy as np
    from app.services.inference.offline.impl.clean import ACTION_LABELS
    c = class_count or len(ACTION_LABELS)
    probs = np.full((frame_count, c), (1.0 - conf) / (c - 1), dtype=np.float32)
    probs[:, label] = conf
    return probs



def _clean_frame(ts):
    """一帧：clean_large=[hand, scope_control_body]，clean_small=[short_brush] → short_brush_cleaning。"""
    large = DetectorOutput(
        boxes=[make_det_box(class_name="hand"),
                    make_det_box(class_name="scope_control_body")],
        metadata={}, timestamp=ts,
    )
    small = DetectorOutput(
        boxes=[make_det_box(class_name="short_brush")], metadata={}, timestamp=ts,
    )
    return {"clean_large": large, "clean_small": small}


class TestCleanSegmenter:
    def test_each_clean_model_uses_own_feature_recipe(self):
        from app.services.inference.offline.impl.clean import (
            CleanASFormerSegmenter,
            CleanBiGRUSegmenter,
            CleanMSTCNBiLSTMSegmenter,
        )
        streams = {
            "clean_large": [_clean_frame(t)["clean_large"] for t in (0.1, 0.2, 0.3, 0.4)],
            "clean_small": [_clean_frame(t)["clean_small"] for t in (0.1, 0.2, 0.3, 0.4)],
        }

        mstcn = CleanMSTCNBiLSTMSegmenter(fps=10.0)
        asformer = CleanASFormerSegmenter(fps=10.0)
        bigru = CleanBiGRUSegmenter(fps=10.0)

        frames = _frames(streams)
        mstcn_input = mstcn.preprocess(frames)
        asformer_input = asformer.preprocess(frames)
        bigru_input = bigru.preprocess(frames)

        assert (mstcn.feature_method, mstcn_input.feature_dim, mstcn_input.feature_version) == (
            "v2", 113, "clean_bbox_v2_top1_impute",
        )
        assert (asformer.feature_method, asformer_input.feature_dim, asformer_input.feature_version) == (
            "business_priors", 121, "clean_bbox_v2_top1_impute+business_priors",
        )
        assert (bigru.feature_method, bigru_input.feature_dim, bigru_input.feature_version) == (
            "window_stats+business_priors", 249, "clean_bbox_v2_top1_impute+center_window+business_priors",
        )
        for mi in (mstcn_input, asformer_input, bigru_input):
            assert mi.frame_count == 4
            assert all(math.isfinite(v) for row in mi.features for v in row)

    def test_segment_with_model_builds_label_probs(self, monkeypatch):
        """带权重路径（前向打桩，不依赖 torch）：逐帧 softmax → TemporalSegment + label_probs 旁路。"""
        from app.services.inference.offline.impl.clean import ACTION_LABELS, CleanMSTCNBiLSTMSegmenter
        seg = CleanMSTCNBiLSTMSegmenter(model_path="unused.pt", min_duration_s=0.1, fps=10.0)
        label = ACTION_LABELS.index("short_brush_cleaning")
        monkeypatch.setattr(seg, "_predict_with_model", lambda mi: _onehot_probs(mi.frame_count, label, 0.9))
        streams = {
            "clean_large": [_clean_frame(t)["clean_large"] for t in (0.1, 0.2, 0.3)],
            "clean_small": [_clean_frame(t)["clean_small"] for t in (0.1, 0.2, 0.3)],
        }
        segs = seg.segment(seg.preprocess(_frames(streams)))
        assert [(s.producer, s.label, s.start, s.end) for s in segs] == [
            ("CleanMSTCNBiLSTMSegmenter", "short_brush_cleaning", 0.1, 0.3),
        ]
        probs = seg.label_probs()
        assert probs.labels == tuple(ACTION_LABELS)
        assert probs.ts.tolist() == [0.1, 0.2, 0.3]
        assert probs.probs.shape == (3, len(ACTION_LABELS))
        assert probs.probs[:, label].tolist() == pytest.approx([0.9] * 3)

    def test_nodep_gru_resamples_to_model_fps_then_builds_226d(self, monkeypatch):
        """15fps 检测 → 7.5fps 入模（只挑真实帧）；前向打桩：6 类 label_probs 落在降采样后的 ts 上。"""
        from app.services.inference.offline.impl.clean import NODEP_GRU_LABELS, CleanNodepGRUSegmenter
        ts = [round(i / 15, 6) for i in range(8)]
        streams = {src: [_clean_frame(t)[src] for t in ts] for src in ("clean_large", "clean_small")}
        seg = CleanNodepGRUSegmenter(model_path="unused.pt", model_input_fps=7.5, min_duration_s=0.0)
        label = NODEP_GRU_LABELS.index("short_brush_cleaning")
        monkeypatch.setattr(seg, "_predict_with_model",
                            lambda mi: _onehot_probs(mi.frame_count, label, 0.9, len(NODEP_GRU_LABELS)))

        mi = seg.preprocess(_frames(streams))
        assert (mi.feature_dim, mi.feature_version, mi.timestamps) == (
            226, "ama-v3-concat23-nodep-226d", ts[::2],
        )
        assert all(math.isfinite(v) for row in mi.features for v in row)
        segs = seg.segment(mi)
        assert [(s.label, s.start, s.end) for s in segs] == [("short_brush_cleaning", ts[0], ts[6])]
        assert seg.label_probs().labels == NODEP_GRU_LABELS

    def test_nodep_gru_rejects_detection_rate_below_model_fps(self):
        from app.services.inference.offline.impl.clean import CleanNodepGRUSegmenter
        ts = [i / 5 for i in range(6)]
        streams = {src: [_clean_frame(t)[src] for t in ts] for src in ("clean_large", "clean_small")}
        with pytest.raises(ValueError, match="低于契约帧率"):
            CleanNodepGRUSegmenter(model_input_fps=7.5).preprocess(_frames(streams))

    def test_causal_windows_end_at_own_frame_and_pad_with_first(self):
        import numpy as np
        from app.services.inference.offline.impl.clean import _causal_windows
        x = np.arange(10, dtype=np.float32).reshape(5, 2)
        w = _causal_windows(x, 3)
        np.testing.assert_array_equal(w[:, -1], x)
        np.testing.assert_array_equal(w[1], x[[0, 0, 1]])


class TestCleanSegmenterThroughRunner:
    def test_clean_segmenter_without_model_path_fails_no_write(self, tmp_storage):
        """CLEAN 分段器不再规则降级；未配 model_path 时硬失败且不落结果。"""
        inference_store.append_detections(make_run(1, 2), [
            make_frame_detection(ts=t, by_source=_clean_frame(t))
            for t in (0.1, 0.2, 0.3, 0.4)
        ])
        offline = {"class": _CLEAN_CLASS,
                   "params": {"min_duration_s": 0.1, "fps": 10.0}}
        with pytest.raises(ValueError, match="model_path"):
            OfflineRunner(config=_config(offline)).run(OfflineRunSpec(task_id=1, step_id=2))
        assert not _probs_path(tmp_storage).exists()
        assert not _facts_path(tmp_storage).exists()

    def test_clean_segmenter_with_model_writes_facts_and_label_probs(self, tmp_storage, monkeypatch):
        """CLEAN 经 Runner 全链路（模型前向打桩）：temporal.jsonl 与 label_probs.npz 均落盘。"""
        from app.services.inference.offline.impl.clean import ACTION_LABELS, _CleanTorchSegmenter
        label = ACTION_LABELS.index("flush")
        monkeypatch.setattr(
            _CleanTorchSegmenter, "_predict_with_model",
            lambda self, mi: _onehot_probs(mi.frame_count, label, 0.8),
        )
        inference_store.append_detections(make_run(1, 2), [
            make_frame_detection(ts=t, by_source=_clean_frame(t)) for t in (0.1, 0.2, 0.3, 0.4)
        ])
        offline = {"class": "app.services.inference.offline.impl.clean.CleanMSTCNBiLSTMSegmenter",
                   "params": {"model_path": "unused.pt", "min_duration_s": 0.1}}
        res = _runner(offline).run(OfflineRunSpec(task_id=1, step_id=2))
        assert (res.status, res.producer, res.segment_count) == ("completed", "CleanMSTCNBiLSTMSegmenter", 1)
        probs = inference_store.read_label_probs(make_run(1, 2))
        assert probs.labels == tuple(ACTION_LABELS)
        assert probs.ts.tolist() == [0.1, 0.2, 0.3, 0.4]
        assert probs.probs.argmax(axis=1).tolist() == [label] * 4

