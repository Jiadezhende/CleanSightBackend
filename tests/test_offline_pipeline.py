"""离线分割入口测试：存储引擎 / 配置工厂 / Runner / mock+clean 策略 / stage 解析 / CLI。

不依赖 GPU / RTSP / DB / 网络；storage 与 config 全用临时件，用例间不串。
"""

import json
import math

import pytest

from factories import make_det_box, make_detector_output, make_frame_detection

from app.domain.detection import DetectorOutput, FrameDetection
from app.domain.temporal import TemporalEvent, TemporalSegment
from app.services.inference.config import InferenceConfig
from app.services.inference.offline.segmenter import OfflineSegmenter
from app.services.inference.offline.runner import OfflineRunner, OfflineRunSpec
from app.services.inference.offline.impl.mock import BrushRulesSegmenter
from app.services.inference.stage_factory import StageFactory
from app.storage import inference as inference_store
from app.utils.exceptions import ValidationError

_MOCK_CLASS = "app.services.inference.offline.impl.mock.BrushRulesSegmenter"
_CLEAN_CLASS = "app.services.inference.offline.impl.clean.CleanSegmenter"


def _frames(per_source):
    """{src: [DetectorOutput 按 ts]} → List[FrameDetection]（按 ts 对齐+升序），供直调 preprocess。"""
    by_ts: dict = {}
    for src, fds in per_source.items():
        for fd in fds:
            by_ts.setdefault(fd.timestamp, {})[src] = fd
    return [FrameDetection(ts=ts, by_source=by_ts[ts]) for ts in sorted(by_ts)]


def _seg(producer="p", label="x", start=0.0, end=1.0):
    return TemporalSegment(producer=producer, label=label, start=start, end=end)


class TestReplaceSegments:
    """Runner 的 read → 合并 → write：数据层只管整体替换，保留谁是这里的事。"""

    @pytest.fixture(autouse=True)
    def _domain_exists(self, tmp_storage):
        """真实调用时已读到检测结果、域目录必在；`_replace_segments` 不建目录（create=False）。"""
        _write_detections(1, 1)

    def test_idempotent_rerun_no_dup(self, tmp_storage):
        facts = [_seg(start=0, end=1)]
        OfflineRunner._replace_segments(1, 1, list(facts))
        OfflineRunner._replace_segments(1, 1, list(facts))
        segs = [f for f in inference_store.read_temporal(1, 1) if isinstance(f, TemporalSegment)]
        assert len(segs) == 1

    def test_all_segments_replaced_eventfact_preserved(self, tmp_storage):
        # 预置：别的 producer 的分段 + 一条 TemporalEvent → 分段整体替换，TemporalEvent 保留
        inference_store.write_temporal(1, 1, [
            _seg(producer="q", start=5, end=6),
            TemporalEvent(producer="s", signal="sig", value=1, ts=1.0),
        ])
        OfflineRunner._replace_segments(1, 1, [_seg(producer="p", start=0, end=1)])
        loaded = inference_store.read_temporal(1, 1)
        assert {f.producer for f in loaded if isinstance(f, TemporalSegment)} == {"p"}
        assert any(isinstance(f, TemporalEvent) for f in loaded)

    def test_empty_clears_segments(self, tmp_storage):
        OfflineRunner._replace_segments(1, 1, [_seg()])
        OfflineRunner._replace_segments(1, 1, [])  # 空 → 清该 step 分段
        segs = [f for f in inference_store.read_temporal(1, 1) if isinstance(f, TemporalSegment)]
        assert segs == []


# ============================ 配置 + 工厂 ============================

def _config(offline):
    return InferenceConfig({"stages": {"2": {
        "detectors": [{"name": "clean_large"}, {"name": "clean_small"}],
        "offline": offline,
    }}})


_OFFLINE_OK = {
    "class": _MOCK_CLASS,
    "params": {"label": "brushing"},
}
# 测试替身策略（定义在本文件下方）：Boom 抛异常，Marker 验证 preprocess 预留层
_BOOM = {"class": "test_offline_pipeline.BoomSegmenter"}
_MARKER = {"class": "test_offline_pipeline.MarkerSegmenter"}


class TestCreateOfflineSegmenter:
    def test_empty_block_returns_none(self):
        # 空块 / 缺省 = 不启用（presence 驱动，无 enabled 开关）
        for offline in ({}, None):
            seg = StageFactory(_config(offline)).create_offline_segmenter("2")
            assert seg is None

    def test_missing_class_fail_fast(self):
        # 非空块即视为有意启用；缺 class fail-fast，不再静默 return None
        with pytest.raises(ValueError, match="class"):
            StageFactory(_config({"params": {"label": "x"}})).create_offline_segmenter("2")

    @pytest.mark.parametrize("class_path", [
        "nonexistent_module.Bad",
        "app.services.inference.offline.impl.mock.NoSuchSegmenter",
    ])
    def test_unimportable_class_fail_fast(self, class_path):
        offline = dict(_OFFLINE_OK, **{"class": class_path})
        with pytest.raises((ImportError, AttributeError)):
            StageFactory(_config(offline)).create_offline_segmenter("2")

    def test_bad_detector_class_fails_fast(self):
        """detector 构造失败即抛（启动 fail-fast），不再记日志后静默少一个流源。"""
        cfg = InferenceConfig({"stages": {"2": {"detectors": [{"name": "d", "class": "nonexistent.Bad"}]}}})
        with pytest.raises(RuntimeError, match="Detector 'd'"):
            StageFactory(cfg).create_detectors_for_stage("2")

    def test_rule_missing_subscribes_fails_fast(self):
        cfg = InferenceConfig({"stages": {"2": {"rules": [{"name": "r", "class": "x.Y"}]}}})
        with pytest.raises(ValueError, match="subscribes"):
            StageFactory(cfg).create_operators_for_stage("2")

    def test_enabled_builds_segmenter(self):
        seg = StageFactory(_config(_OFFLINE_OK)).create_offline_segmenter("2")
        assert isinstance(seg, BrushRulesSegmenter)
        assert seg.name == "BrushRulesSegmenter"  # producer = 类名
        assert seg.label == "brushing"


# ============================ 离线可跑校验 ============================

class TestRequireOffline:
    """离线不兜底 MOCK：未定义 / offline 为空的 step 都是参数错误。"""

    def test_configured_returns_stage_key(self):
        assert _config(_OFFLINE_OK).require_offline(2) == "2"

    @pytest.mark.parametrize("cfg,step_id,reason", [
        (_config(_OFFLINE_OK), 999, "未在推理配置中定义"),
        (_config(_OFFLINE_OK), -1, "未在推理配置中定义"),   # 无 MOCK 兜底
        (_config({}), 2, "未配置离线模型"),
    ])
    def test_unrunnable_rejected(self, cfg, step_id, reason):
        with pytest.raises(ValidationError, match=reason):
            cfg.require_offline(step_id)


# ============================ BrushRulesSegmenter（MOCK 链路 stand-in） ============================

class TestBrushRulesSegmenter:
    def test_presence_runs_to_segments(self):
        seg = BrushRulesSegmenter()
        streams = {"a": [
            make_detector_output(n=1, ts=1.0),   # active
            make_detector_output(n=1, ts=2.0),   # active
            make_detector_output(n=0, ts=3.0),   # idle → 断段
            make_detector_output(n=1, ts=4.0),   # active（新段）
        ]}
        segs = seg.segment(seg.preprocess(_frames(streams)))
        assert [(s.start, s.end) for s in segs] == [(1.0, 2.0), (4.0, 4.0)]
        assert all(s.producer == "BrushRulesSegmenter" for s in segs)

    def test_min_frames_drops_short_runs(self):
        seg = BrushRulesSegmenter(min_frames=2)
        streams = {"a": [
            make_detector_output(n=1, ts=1.0),   # 单帧段，min_frames=2 丢弃
            make_detector_output(n=0, ts=2.0),
        ]}
        assert seg.segment(seg.preprocess(_frames(streams))) == []

    def test_label_probs_none(self):
        """规则型无逐帧概率：label_probs 恒 None（Runner 据此不落 label_probs.npz）。"""
        seg = BrushRulesSegmenter()
        seg.segment(seg.preprocess(_frames({"a": [make_detector_output(n=1, ts=1.0)]})))
        assert seg.label_probs() is None


# ============================ CleanSegmenter（CLEAN baseline） ============================

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
    def test_flatten_preprocess_to_segments(self):
        from app.services.inference.offline.impl.clean import CleanSegmenter, ModelInput
        seg = CleanSegmenter(min_duration_s=0.1, fps=10.0)
        streams = {
            "clean_large": [_clean_frame(t)["clean_large"] for t in (0.1, 0.2, 0.3, 0.4)],
            "clean_small": [_clean_frame(t)["clean_small"] for t in (0.1, 0.2, 0.3, 0.4)],
        }
        mi = seg.preprocess(_frames(streams))
        assert isinstance(mi, ModelInput)
        assert mi.frame_count == 4 and mi.feature_dim == 113  # v2: hand top-2 + top-1/impute/relations
        assert mi.feature_version == "clean_bbox_v2_top1_impute"
        assert all(math.isfinite(v) for row in mi.features for v in row)
        with pytest.raises(ValueError, match="model_path"):
            seg.segment(mi)

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

    def test_no_model_path_hard_fails_without_label_probs(self):
        from app.services.inference.offline.impl.clean import CleanSegmenter
        seg = CleanSegmenter(min_duration_s=0.1, fps=10.0)
        streams = {
            "clean_large": [_clean_frame(t)["clean_large"] for t in (0.1, 0.2, 0.3)],
            "clean_small": [_clean_frame(t)["clean_small"] for t in (0.1, 0.2, 0.3)],
        }
        with pytest.raises(ValueError, match="model_path"):
            seg.segment(seg.preprocess(_frames(streams)))
        assert seg.label_probs() is None

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


# ============================ Runner ============================

def _runner(offline):
    return OfflineRunner(config=_config(offline))


def _facts_path(root, task_id=1, step_id=2):
    return root / str(task_id) / str(step_id) / "inference" / "temporal.jsonl"


def _probs_path(root, task_id=1, step_id=2):
    return root / str(task_id) / str(step_id) / "inference" / "label_probs.npz"


def _write_detections(task_id, step_id):
    """经数据层预置两帧双源检测结果（storage 根已由 tmp_storage fixture 指到临时目录）。"""
    inference_store.append_detections(task_id, step_id, [
        make_frame_detection(ts=ts, by_source={
            "clean_large": make_detector_output(n=1, ts=ts),
            "clean_small": make_detector_output(n=1, ts=ts),
        })
        for ts in (1.0, 2.0)
    ])


class TestOfflineRunner:
    def test_unconfigured_step_raises_no_write(self, tmp_storage):
        _write_detections(1, 999)
        with pytest.raises(ValidationError):
            _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=999))
        assert not _facts_path(tmp_storage, step_id=999).exists()

    def test_offline_disabled_raises(self, tmp_storage):
        with pytest.raises(ValidationError, match="未配置离线模型"):
            _runner({}).run(OfflineRunSpec(task_id=1, step_id=2))

    def test_missing_input_skipped_no_write(self, tmp_storage):
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "skipped"
        assert not _facts_path(tmp_storage).exists()

    def test_completed_writes_facts(self, tmp_storage):
        _write_detections(1, 2)
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "completed"
        assert res.producer == "BrushRulesSegmenter"
        assert res.segment_count == 1
        segs = [f for f in inference_store.read_temporal(1, 2) if isinstance(f, TemporalSegment)]
        assert len(segs) == 1
        assert segs[0].producer == "BrushRulesSegmenter"
        assert segs[0].label == "brushing"
        # BrushRulesSegmenter.label_probs() 为 None → 不落 label_probs.npz
        assert not _probs_path(tmp_storage).exists()

    def test_rerun_idempotent(self, tmp_storage):
        _write_detections(1, 2)
        r = _runner(_OFFLINE_OK)
        r.run(OfflineRunSpec(task_id=1, step_id=2))
        r.run(OfflineRunSpec(task_id=1, step_id=2))
        segs = [f for f in inference_store.read_temporal(1, 2) if isinstance(f, TemporalSegment)]
        assert len(segs) == 1

    def test_strategy_exception_propagates_no_write(self, tmp_storage):
        _write_detections(1, 2)
        with pytest.raises(RuntimeError):
            _runner(_BOOM).run(OfflineRunSpec(task_id=1, step_id=2))
        assert not _facts_path(tmp_storage).exists()

    def test_preprocess_seam_invoked(self, tmp_storage):
        _write_detections(1, 2)
        res = _runner(_MARKER).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "completed"
        assert res.segment_count == 1

    def test_clean_segmenter_without_model_path_fails_no_write(self, tmp_storage):
        """CleanSegmenter 不再规则降级；未配 model_path 时硬失败且不落结果。"""
        inference_store.append_detections(1, 2, [
            make_frame_detection(ts=t, by_source=_clean_frame(t))
            for t in (0.1, 0.2, 0.3, 0.4)
        ])
        offline = dict(_OFFLINE_OK, **{"class": _CLEAN_CLASS,
                                       "params": {"min_duration_s": 0.1, "fps": 10.0}})
        with pytest.raises(ValueError, match="model_path"):
            OfflineRunner(config=_config(offline)).run(OfflineRunSpec(task_id=1, step_id=2))
        assert not _probs_path(tmp_storage).exists()
        assert not _facts_path(tmp_storage).exists()

    def test_partial_sources_not_skipped(self, tmp_storage):
        """跳过判据只看检测序列是否为空，不再按 source 名逐一检查。"""
        inference_store.append_detections(1, 2, [
            make_frame_detection(ts=1.0, by_source={"other": make_detector_output(n=1, ts=1.0)})
        ])
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "completed"
        assert res.segment_count == 1

    def test_model_swap_replaces_old_segments_keeps_eventfact(self, tmp_storage):
        """换模型重跑：旧类名的分段整体被替换，TemporalEvent 保留。"""
        _write_detections(1, 2)
        inference_store.write_temporal(1, 2, [TemporalEvent(producer="clean_monitor", signal="sig", value=1, ts=1.0)])
        assert _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2)).producer == "BrushRulesSegmenter"
        res = _runner(_MARKER).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.producer == "MarkerSegmenter"
        loaded = inference_store.read_temporal(1, 2)
        assert {f.producer for f in loaded if isinstance(f, TemporalSegment)} == {"MarkerSegmenter"}
        assert [f.producer for f in loaded if isinstance(f, TemporalEvent)] == ["clean_monitor"]

    def test_clean_segmenter_with_model_writes_facts_and_label_probs(self, tmp_storage, monkeypatch):
        """CLEAN 经 Runner 全链路（模型前向打桩）：temporal.jsonl 与 label_probs.npz 均落盘。"""
        from app.services.inference.offline.impl.clean import ACTION_LABELS, _CleanTorchSegmenter
        label = ACTION_LABELS.index("flush")
        monkeypatch.setattr(
            _CleanTorchSegmenter, "_predict_with_model",
            lambda self, mi: _onehot_probs(mi.frame_count, label, 0.8),
        )
        inference_store.append_detections(1, 2, [
            make_frame_detection(ts=t, by_source=_clean_frame(t)) for t in (0.1, 0.2, 0.3, 0.4)
        ])
        offline = {"class": "app.services.inference.offline.impl.clean.CleanMSTCNBiLSTMSegmenter",
                   "params": {"model_path": "unused.pt", "min_duration_s": 0.1}}
        res = _runner(offline).run(OfflineRunSpec(task_id=1, step_id=2))
        assert (res.status, res.producer, res.segment_count) == ("completed", "CleanMSTCNBiLSTMSegmenter", 1)
        probs = inference_store.read_label_probs(1, 2)
        assert probs.labels == tuple(ACTION_LABELS)
        assert probs.ts.tolist() == [0.1, 0.2, 0.3, 0.4]
        assert probs.probs.argmax(axis=1).tolist() == [label] * 4

    def test_bad_label_probs_shape_skips_bypass_keeps_facts(self, tmp_storage):
        """旁路形状不一致：不落 npz，事实照常写（旁路不影响主结果）。"""
        _write_detections(1, 2)
        offline = {"class": f"{__name__}.BadProbsSegmenter"}
        res = _runner(offline).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "completed"
        assert not _probs_path(tmp_storage).exists()
        assert _facts_path(tmp_storage).exists()


class BoomSegmenter(OfflineSegmenter):
    def preprocess(self, frames):
        return frames

    def segment(self, model_input):
        raise RuntimeError("boom")


class MarkerSegmenter(OfflineSegmenter):
    """验证 preprocess 预留层被 runner 调用：preprocess 打标，segment 据标产段。"""

    def preprocess(self, frames):
        return {"marked": True, "frames": frames}

    def segment(self, model_input):
        assert model_input.get("marked") is True  # runner 确实先调了 preprocess
        return [TemporalSegment(producer=self.name, label="m", start=0.0, end=1.0)]


# ============================ CLI ============================

class TestCli:
    def test_run_completed_json_last_line(self, tmp_storage, monkeypatch, capsys):
        """stdout 末行恒为结果 JSON（作业服务按此解析）。

        默认路径：OfflineRunner() 用 settings.storage_base_dir（tmp_storage 已指临时目录）
        + runner 内 load_stage_config（monkeypatch 成临时 config，绕开单例）。
        """
        from app.services.inference.offline import runner as runner_mod
        _write_detections(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2"])
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 0
        assert json.loads(last) == {
            "status": "completed", "producer": "BrushRulesSegmenter", "segment_count": 1, "message": "",
        }

    def test_run_strategy_error_exit_nonzero(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import runner as runner_mod
        _write_detections(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_BOOM))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 1
        assert payload["status"] == "error" and payload["message"] == "boom"

    def test_run_unconfigured_step_error(self, tmp_storage, monkeypatch, capsys):
        """未配置的 step 不兜底 MOCK：退出码 1，末行 JSON status=error。"""
        from app.services.inference.offline import runner as runner_mod
        _write_detections(1, 7)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        assert cli.main(["run", "--task-id", "1", "--step-id", "7"]) == 1
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["status"] == "error" and "未在推理配置中定义" in payload["message"]

    def test_query_roundtrip(self, tmp_storage, monkeypatch, capsys):
        """run 写出 facts 后，query 子命令能读回时间线。"""
        from app.services.inference.offline import runner as runner_mod
        _write_detections(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        assert cli.main(["run", "--task-id", "1", "--step-id", "2"]) == 0
        capsys.readouterr()  # 清 run 的输出
        rc = cli.main(["query", "--task-id", "1", "--step-id", "2"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["task_id"] == 1
        assert [row["label"] for row in payload["timeline"]] == ["brushing"]

    def test_no_online_imports(self):
        """入口模块不得拉起在线服务模块。"""
        import importlib
        import sys
        for m in ("app.services.inference.online.manager", "app.main"):
            sys.modules.pop(m, None)
        importlib.import_module("app.services.inference.offline.cli")
        assert "app.main" not in sys.modules


def _onehot_probs(frame_count, label, conf):
    """打桩用逐帧 softmax：目标列 conf，其余列均分余量。"""
    import numpy as np
    from app.services.inference.offline.impl.clean import ACTION_LABELS
    c = len(ACTION_LABELS)
    probs = np.full((frame_count, c), (1.0 - conf) / (c - 1), dtype=np.float32)
    probs[:, label] = conf
    return probs


class BadProbsSegmenter(OfflineSegmenter):
    """label_probs 行数与 ts 对不上的策略：验证 Runner 旁路防护。"""

    def preprocess(self, frames):
        return frames

    def segment(self, model_input):
        return []

    def label_probs(self):
        import numpy as np
        from app.domain.temporal import LabelProbs
        return LabelProbs(ts=np.array([1.0, 2.0]), probs=np.zeros((3, 2)), labels=("a", "b"))


# ============================ 换代校验 ============================

def _late_frame(ts=9.0):
    return make_frame_detection(ts=ts, by_source={"clean_large": make_detector_output(n=1, ts=ts)})


class _ProbsSegmenter(OfflineSegmenter):
    """产一段 + 一份合法 label_probs；子类在 segment() 里改输入，验证两份产物都不落。"""

    def preprocess(self, frames):
        return frames

    def mutate(self):
        raise NotImplementedError

    def segment(self, model_input):
        self.mutate()
        return [TemporalSegment(producer=self.name, label="m", start=0.0, end=1.0)]

    def label_probs(self):
        import numpy as np
        from app.domain.temporal import LabelProbs
        return LabelProbs(ts=np.array([1.0, 2.0]), probs=np.ones((2, 1)), labels=("a",))


class AppendDuringSegmentSegmenter(_ProbsSegmenter):
    """运行期间残批迟到落盘（输入未封口）。"""

    def mutate(self):
        inference_store.append_detections(1, 2, [_late_frame()])


class RegenDuringSegmentSegmenter(_ProbsSegmenter):
    """运行期间同 step 新一代 run 开写：recording 整域删后重建。"""

    def mutate(self):
        inference_store.delete(1, 2)
        inference_store.append_detections(1, 2, [_late_frame()])


class TestOfflineRunnerSupersede:
    """输入戳在读 / 算期间变了 → superseded，temporal 与 label_probs 都不写。"""

    @pytest.mark.parametrize("cls", ["AppendDuringSegmentSegmenter", "RegenDuringSegmentSegmenter"])
    def test_input_changed_during_segment(self, tmp_storage, cls):
        _write_detections(1, 2)
        res = _runner({"class": f"{__name__}.{cls}"}).run(OfflineRunSpec(task_id=1, step_id=2))
        assert (res.status, res.segment_count) == ("superseded", 0)
        assert not _facts_path(tmp_storage).exists()
        assert not _probs_path(tmp_storage).exists()

    def test_input_changed_during_read(self, tmp_storage, monkeypatch):
        _write_detections(1, 2)
        real_read = inference_store.read_detections

        def read_then_append(task_id, step_id):
            frames = real_read(task_id, step_id)
            inference_store.append_detections(task_id, step_id, [_late_frame()])
            return frames

        monkeypatch.setattr(inference_store, "read_detections", read_then_append)
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "superseded"
        assert not _facts_path(tmp_storage).exists()

    def test_superseded_keeps_previous_result(self, tmp_storage):
        """上一次的结果原样保留（superseded 不是「清空」）。"""
        _write_detections(1, 2)
        _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        before = inference_store.read_temporal(1, 2)
        res = _runner({"class": f"{__name__}.AppendDuringSegmentSegmenter"}).run(
            OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "superseded"
        assert inference_store.read_temporal(1, 2) == before


class TestOfflineRunnerNoResurrect:
    """戳核对通过之后 step 才被 TTL 回收：superseded，不重建目录（无僵尸 step）。"""

    def test_reclaimed_before_facts_write(self, tmp_storage, monkeypatch):
        from app.storage import tasks
        _write_detections(1, 2)
        monkeypatch.setattr(inference_store, "read_temporal",
                            lambda t, s: (tasks.delete_step(t, s), [])[1])
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "superseded"
        assert not (tmp_storage / "1").exists()

    def test_reclaimed_before_label_probs_write(self, tmp_storage, monkeypatch):
        from app.storage import tasks
        _write_detections(1, 2)
        real_write = inference_store.write_label_probs

        def reclaim_then_write(t, s, probs, **kw):
            tasks.delete_step(t, s)
            return real_write(t, s, probs, **kw)

        monkeypatch.setattr(inference_store, "write_label_probs", reclaim_then_write)
        res = _runner({"class": f"{__name__}.StaticProbsSegmenter"}).run(
            OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "superseded"
        assert not (tmp_storage / "1").exists()


class StaticProbsSegmenter(_ProbsSegmenter):
    """产一段 + 合法 label_probs，不改输入。"""

    def mutate(self):
        pass
