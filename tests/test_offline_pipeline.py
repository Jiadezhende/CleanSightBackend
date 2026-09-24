"""离线分割入口测试：存储引擎 / 配置工厂 / Runner / mock+clean 策略 / stage 解析 / CLI。

不依赖 GPU / RTSP / DB / 网络；storage 与 config 全用临时件，用例间不串。
"""

import json
import math

import pytest

from factories import make_det_box, make_detector_output, make_frame_detection

from app.domain.detection import DetectorOutput, FrameDetection
from app.domain.fact import EventFact, SegmentFact
from app.services.inference.config import InferenceConfig
from app.services.inference.offline.segmenter import OfflineSegmenter
from app.services.inference.offline.runner import OfflineRunner, OfflineRunSpec
from app.services.inference.offline.impl.mock import BrushRulesSegmenter
from app.services.inference.stage_factory import StageFactory
from app.storage import inference as inference_store

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
    return SegmentFact(producer=producer, label=label, start=start, end=end)


class TestReplaceOwnSegments:
    """Runner 的 read → 合并 → write：数据层只管整体替换，保留谁是这里的事。"""

    def test_idempotent_rerun_no_dup(self, tmp_storage):
        facts = [_seg(start=0, end=1)]
        OfflineRunner._replace_own_segments(1, 1, "p", list(facts))
        OfflineRunner._replace_own_segments(1, 1, "p", list(facts))
        segs = [f for f in inference_store.read_facts(1, 1) if isinstance(f, SegmentFact)]
        assert len(segs) == 1

    def test_other_producer_and_eventfact_preserved(self, tmp_storage):
        # 预置：别的 producer 的分段 + 一条 EventFact
        inference_store.write_facts(1, 1, [
            _seg(producer="q", start=5, end=6),
            EventFact(producer="s", signal="sig", value=1, ts=1.0),
        ])
        OfflineRunner._replace_own_segments(1, 1, "p", [_seg(producer="p", start=0, end=1)])
        loaded = inference_store.read_facts(1, 1)
        assert {f.producer for f in loaded if isinstance(f, SegmentFact)} == {"p", "q"}
        assert any(isinstance(f, EventFact) for f in loaded)

    def test_empty_clears_own_producer(self, tmp_storage):
        OfflineRunner._replace_own_segments(1, 1, "p", [_seg()])
        OfflineRunner._replace_own_segments(1, 1, "p", [])  # 空 → 清该 producer
        segs = [f for f in inference_store.read_facts(1, 1) if isinstance(f, SegmentFact)]
        assert segs == []


# ============================ 配置 + 工厂 ============================

def _config(offline):
    return InferenceConfig({"stages": {"2": {
        "detectors": [{"name": "clean_large"}, {"name": "clean_small"}],
        "offline": offline,
    }}})


_OFFLINE_OK = {
    "name": "clean_seg",
    "subscribes": ["clean_large", "clean_small"],
    "class": _MOCK_CLASS,
    "params": {"label": "brushing"},
}


class TestCreateOfflineSegmenter:
    def test_empty_block_returns_none(self):
        # 空块 / 缺省 = 不启用（presence 驱动，无 enabled 开关）
        for offline in ({}, None):
            seg = StageFactory(_config(offline)).create_offline_segmenter("2")
            assert seg is None

    def test_nonempty_without_required_fail_fast(self):
        # 非空块即视为有意启用；缺必填字段 fail-fast，不再静默 return None
        with pytest.raises(ValueError):
            StageFactory(_config({"params": {"label": "x"}})).create_offline_segmenter("2")

    def test_enabled_builds_segmenter(self):
        seg = StageFactory(_config(_OFFLINE_OK)).create_offline_segmenter("2")
        assert isinstance(seg, BrushRulesSegmenter)
        assert seg.name == "clean_seg"
        assert seg.subscribes == ["clean_large", "clean_small"]
        assert seg.label == "brushing"

    @pytest.mark.parametrize("missing", ["name", "subscribes", "class"])
    def test_missing_required_fail_fast(self, missing):
        offline = dict(_OFFLINE_OK)
        offline.pop(missing)
        with pytest.raises(ValueError):
            StageFactory(_config(offline)).create_offline_segmenter("2")

    def test_unknown_detector_subscribe_fail_fast(self):
        offline = dict(_OFFLINE_OK, subscribes=["clean_large", "ghost"])
        with pytest.raises(ValueError):
            StageFactory(_config(offline)).create_offline_segmenter("2")

    def test_reserved_param_fail_fast(self):
        offline = dict(_OFFLINE_OK, params={"name": "dup"})
        with pytest.raises(ValueError):
            StageFactory(_config(offline)).create_offline_segmenter("2")

    def test_override_class(self):
        offline = dict(_OFFLINE_OK, **{"class": "nonexistent.Bad"})
        seg = StageFactory(_config(offline)).create_offline_segmenter(
            "2", override_class=_MOCK_CLASS
        )
        assert isinstance(seg, BrushRulesSegmenter)


# ============================ stage 解析回退 ============================

class TestResolveStage:
    def test_hit_returns_identity_miss_falls_back_mock(self):
        cfg = InferenceConfig({"stages": {
            "2": {"detectors": [{"name": "clean_large"}]},
            "MOCK": {"detectors": [{"name": "mock"}]},
        }})
        assert cfg.resolve_stage(2) == "2"
        assert cfg.resolve_stage("2") == "2"
        assert cfg.resolve_stage(-1) == "MOCK"       # 未配数字 → 回退
        assert cfg.resolve_stage(999) == "MOCK"


# ============================ BrushRulesSegmenter（MOCK 链路 stand-in） ============================

class TestBrushRulesSegmenter:
    def test_presence_runs_to_segments(self):
        seg = BrushRulesSegmenter(name="p", subscribes=["a"])
        streams = {"a": [
            make_detector_output(n=1, ts=1.0),   # active
            make_detector_output(n=1, ts=2.0),   # active
            make_detector_output(n=0, ts=3.0),   # idle → 断段
            make_detector_output(n=1, ts=4.0),   # active（新段）
        ]}
        segs = seg.segment(seg.preprocess(_frames(streams)))
        assert [(s.start, s.end) for s in segs] == [(1.0, 2.0), (4.0, 4.0)]
        assert all(s.producer == "p" for s in segs)

    def test_min_frames_drops_short_runs(self):
        seg = BrushRulesSegmenter(name="p", subscribes=["a"], min_frames=2)
        streams = {"a": [
            make_detector_output(n=1, ts=1.0),   # 单帧段，min_frames=2 丢弃
            make_detector_output(n=0, ts=2.0),
        ]}
        assert seg.segment(seg.preprocess(_frames(streams))) == []

    def test_debug_result_none(self):
        """presence 型无逐帧语义：debug_result 恒 None（Runner 据此不落逐帧 JSON）。"""
        seg = BrushRulesSegmenter(name="p", subscribes=["a"])
        seg.segment(seg.preprocess(_frames({"a": [make_detector_output(n=1, ts=1.0)]})))
        assert seg.debug_result() is None


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
        seg = CleanSegmenter(name="clean_seg", subscribes=["clean_large", "clean_small"],
                             min_duration_s=0.1, fps=10.0)
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

        mstcn = CleanMSTCNBiLSTMSegmenter(name="m", subscribes=["clean_large", "clean_small"], fps=10.0)
        asformer = CleanASFormerSegmenter(name="a", subscribes=["clean_large", "clean_small"], fps=10.0)
        bigru = CleanBiGRUSegmenter(name="b", subscribes=["clean_large", "clean_small"], fps=10.0)

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

    def test_no_model_path_hard_fails_without_debug_result(self):
        from app.services.inference.offline.impl.clean import CleanSegmenter
        seg = CleanSegmenter(name="clean_seg", subscribes=["clean_large", "clean_small"],
                             min_duration_s=0.1, fps=10.0)
        streams = {
            "clean_large": [_clean_frame(t)["clean_large"] for t in (0.1, 0.2, 0.3)],
            "clean_small": [_clean_frame(t)["clean_small"] for t in (0.1, 0.2, 0.3)],
        }
        with pytest.raises(ValueError, match="model_path"):
            seg.segment(seg.preprocess(_frames(streams)))
        assert seg.debug_result() is None


# ============================ Runner ============================

def _runner(offline):
    return OfflineRunner(config=_config(offline))


def _facts_path(root, task_id=1, step_id=2):
    return root / str(task_id) / str(step_id) / "inference" / "facts.jsonl"


def _debug_path(root, task_id=1, step_id=2):
    return root / str(task_id) / str(step_id) / "inference" / "offline_debug.json"


def _write_features(task_id, step_id):
    """经数据层预置两帧双源特征（storage 根已由 tmp_storage fixture 指到临时目录）。"""
    inference_store.append_features(task_id, step_id, [
        make_frame_detection(ts=ts, by_source={
            "clean_large": make_detector_output(n=1, ts=ts),
            "clean_small": make_detector_output(n=1, ts=ts),
        })
        for ts in (1.0, 2.0)
    ])


class TestOfflineRunner:
    def test_unknown_stage_skipped(self, tmp_storage):
        r = OfflineRunner(config=_config(_OFFLINE_OK))
        res = r.run(OfflineRunSpec(task_id=1, step_id=999))
        assert res.status == "skipped"

    def test_offline_disabled_skipped(self, tmp_storage):
        res = _runner({}).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "skipped"

    def test_missing_input_skipped_no_write(self, tmp_storage):
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "skipped"
        assert not _facts_path(tmp_storage).exists()

    def test_completed_writes_facts(self, tmp_storage):
        _write_features(1, 2)
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "completed"
        assert res.producer == "clean_seg"
        assert res.segment_count == 1
        segs = [f for f in inference_store.read_facts(1, 2) if isinstance(f, SegmentFact)]
        assert len(segs) == 1
        assert segs[0].producer == "clean_seg"
        assert segs[0].label == "brushing"
        # BrushRulesSegmenter.debug_result() 为 None → 不落逐帧 JSON
        assert not _debug_path(tmp_storage).exists()

    def test_rerun_idempotent(self, tmp_storage):
        _write_features(1, 2)
        r = _runner(_OFFLINE_OK)
        r.run(OfflineRunSpec(task_id=1, step_id=2))
        r.run(OfflineRunSpec(task_id=1, step_id=2))
        segs = [f for f in inference_store.read_facts(1, 2) if isinstance(f, SegmentFact)]
        assert len(segs) == 1

    def test_strategy_exception_propagates_no_write(self, tmp_storage):
        _write_features(1, 2)
        r = OfflineRunner(config=_config(dict(_OFFLINE_OK, params={})))
        with pytest.raises(RuntimeError):
            r.run(OfflineRunSpec(task_id=1, step_id=2,
                                 strategy="test_offline_pipeline.BoomSegmenter"))
        assert not _facts_path(tmp_storage).exists()

    def test_preprocess_seam_invoked(self, tmp_storage):
        _write_features(1, 2)
        r = OfflineRunner(config=_config(dict(_OFFLINE_OK, params={})))
        res = r.run(OfflineRunSpec(task_id=1, step_id=2,
                                   strategy="test_offline_pipeline.MarkerSegmenter"))
        assert res.status == "completed"
        assert res.segment_count == 1

    def test_clean_segmenter_without_model_path_fails_no_write(self, tmp_storage):
        """CleanSegmenter 不再规则降级；未配 model_path 时硬失败且不落结果。"""
        inference_store.append_features(1, 2, [
            make_frame_detection(ts=t, by_source=_clean_frame(t))
            for t in (0.1, 0.2, 0.3, 0.4)
        ])
        offline = dict(_OFFLINE_OK, **{"class": _CLEAN_CLASS,
                                       "params": {"min_duration_s": 0.1, "fps": 10.0}})
        with pytest.raises(ValueError, match="model_path"):
            OfflineRunner(config=_config(offline)).run(OfflineRunSpec(task_id=1, step_id=2))
        assert not _debug_path(tmp_storage).exists()
        assert not _facts_path(tmp_storage).exists()

    def test_resolve_stage_fallback_to_mock(self, tmp_storage):
        """未配数字 step_id(-1) 经 resolve_stage 回退 MOCK.offline，读数字 -1 分区、completed。"""
        cfg = InferenceConfig({"stages": {"MOCK": {
            "detectors": [{"name": "mock"}],
            "offline": {"name": "mock_offline", "subscribes": ["mock"],
                        "class": _MOCK_CLASS, "params": {"label": "mock_action", "min_frames": 1}},
        }}})
        # MockDetector 纯透传：空检测帧 → 0 段，但链路走通
        inference_store.append_features(1, -1, [
            make_frame_detection(ts=1.0, by_source={"mock": make_detector_output(n=0, ts=1.0)})
        ])
        res = OfflineRunner(config=cfg).run(OfflineRunSpec(task_id=1, step_id=-1))
        assert res.status == "completed"
        assert res.producer == "mock_offline"
        assert res.segment_count == 0


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
        return [SegmentFact(producer=self.name, label="m", start=0.0, end=1.0)]


# ============================ CLI ============================

class TestCli:
    def test_run_completed_exit_zero(self, tmp_storage, monkeypatch, capsys):
        # 默认路径：OfflineRunner() 用 settings.storage_base_dir（tmp_storage 已指临时目录）
        # + runner 内 load_stage_config（monkeypatch 成临时 config，绕开单例）。
        from app.services.inference.offline import runner as runner_mod
        _write_features(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "completed" in out

    def test_run_error_exit_nonzero(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import runner as runner_mod
        _write_features(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2",
                       "--strategy", "test_offline_pipeline.BoomSegmenter"])
        assert rc == 1
        assert "error" in capsys.readouterr().out

    def test_query_roundtrip(self, tmp_storage, monkeypatch, capsys):
        """run 写出 facts 后，query 子命令能读回时间线。"""
        from app.services.inference.offline import runner as runner_mod
        _write_features(1, 2)
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
        for m in ("app.services.inference.manager", "app.main"):
            sys.modules.pop(m, None)
        importlib.import_module("app.services.inference.offline.cli")
        assert "app.main" not in sys.modules
