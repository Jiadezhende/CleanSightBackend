"""离线分割入口 v1 测试：存储引擎 / 配置工厂 / Runner / 占位策略 / CLI。

不依赖 GPU / RTSP / DB / 网络；storage 与 config 全用临时件，用例间不串。
"""

import pytest

from factories import make_frame_detections, make_frame_inference

from app.services.inference.config import InferenceConfig
from app.services.inference.feature.store import FactLedger, FeatureStore
from app.services.inference.models import EventFact, SegmentFact
from app.services.inference.offline.base import OfflineSegmenter
from app.services.inference.offline.runner import OfflineRunner, OfflineRunSpec
from app.services.inference.offline.segmenters.clean_action import CleanActionSegmenter
from app.services.inference.stage_factory import StageFactory


# ============================ 存储引擎 ============================

def _append_frame(store: FeatureStore, task_id, step_id, ts, detectors):
    """经 FeatureStore.append 写一帧（detectors: name -> FrameDetections）。"""
    store.append(task_id, step_id, make_frame_inference(cq=None, ts=ts, detectors=detectors))


class TestLoadMany:
    def test_single_scan_multi_source_and_empty_frames_kept(self, tmp_path):
        store = FeatureStore(tmp_path)
        # ts=1: 两源都有；ts=2: large 空帧(0 检测)、small 有；ts=3: 只有 large
        _append_frame(store, 7, 2, 1.0, {
            "clean_large": make_frame_detections(n=1, ts=1.0),
            "clean_small": make_frame_detections(n=2, ts=1.0),
        })
        _append_frame(store, 7, 2, 2.0, {
            "clean_large": make_frame_detections(n=0, ts=2.0),
            "clean_small": make_frame_detections(n=1, ts=2.0),
        })
        _append_frame(store, 7, 2, 3.0, {
            "clean_large": make_frame_detections(n=1, ts=3.0),
        })
        out = store.load_many(7, 2, ["clean_large", "clean_small"])
        # large 出现在 ts 1/2/3（含 2 的空帧）；small 出现在 1/2
        assert [fd.timestamp for fd in out["clean_large"]] == [1.0, 2.0, 3.0]
        assert [fd.timestamp for fd in out["clean_small"]] == [1.0, 2.0]
        assert len(out["clean_large"][1].detections) == 0  # 空检测帧保留

    def test_missing_file_returns_empty_lists(self, tmp_path):
        out = FeatureStore(tmp_path).load_many(1, 1, ["a", "b"])
        assert out == {"a": [], "b": []}

    def test_sorted_by_ts(self, tmp_path):
        store = FeatureStore(tmp_path)
        for ts in (3.0, 1.0, 2.0):
            _append_frame(store, 1, 1, ts, {"a": make_frame_detections(n=1, ts=ts)})
        out = store.load_many(1, 1, ["a"])
        assert [fd.timestamp for fd in out["a"]] == [1.0, 2.0, 3.0]

    def test_corrupt_line_skipped(self, tmp_path):
        store = FeatureStore(tmp_path)
        _append_frame(store, 1, 1, 1.0, {"a": make_frame_detections(n=1, ts=1.0)})
        store.flush(1, 1)
        path = tmp_path / "1" / "1" / "features.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write("{ not json\n")
        _append_frame(store, 1, 1, 2.0, {"a": make_frame_detections(n=1, ts=2.0)})
        out = store.load_many(1, 1, ["a"])
        assert [fd.timestamp for fd in out["a"]] == [1.0, 2.0]

    def test_load_delegates_to_load_many(self, tmp_path):
        store = FeatureStore(tmp_path)
        _append_frame(store, 1, 1, 1.0, {"a": make_frame_detections(n=1, ts=1.0)})
        assert len(store.load(1, 1, "a")) == 1


def _seg(source="p", label="x", start=0.0, end=1.0, producer=None):
    meta = {"producer": producer} if producer else {}
    return SegmentFact(source=source, label=label, start=start, end=end, meta=meta)


class TestReplaceSegments:
    def test_idempotent_rerun_no_dup(self, tmp_path):
        ledger = FactLedger(tmp_path)
        facts = [_seg(producer="p", start=0, end=1)]
        ledger.replace_segments(1, 1, "p", list(facts))
        ledger.replace_segments(1, 1, "p", list(facts))
        loaded = ledger.load(1, 1)
        segs = [f for f in loaded if isinstance(f, SegmentFact)]
        assert len(segs) == 1

    def test_other_producer_and_eventfact_preserved(self, tmp_path):
        ledger = FactLedger(tmp_path)
        # 预置：别的 producer 的分段 + 一条 EventFact
        ledger.append(1, 1, [
            _seg(source="q", producer="q", start=5, end=6),
            EventFact(source="s", signal="sig", value=1, ts=1.0),
        ])
        ledger.replace_segments(1, 1, "p", [_seg(source="p", producer="p", start=0, end=1)])
        loaded = ledger.load(1, 1)
        producers = {f.meta.get("producer") for f in loaded if isinstance(f, SegmentFact)}
        assert producers == {"p", "q"}
        assert any(isinstance(f, EventFact) for f in loaded)

    def test_empty_clears_own_producer(self, tmp_path):
        ledger = FactLedger(tmp_path)
        ledger.replace_segments(1, 1, "p", [_seg(source="p", producer="p")])
        ledger.replace_segments(1, 1, "p", [])  # 空 → 清该 producer
        segs = [f for f in ledger.load(1, 1) if isinstance(f, SegmentFact)]
        assert segs == []

    def test_write_failure_keeps_old_file(self, tmp_path, monkeypatch):
        ledger = FactLedger(tmp_path)
        ledger.replace_segments(1, 1, "p", [_seg(source="p", producer="p", start=0, end=1)])
        path = tmp_path / "1" / "1" / "facts.jsonl"
        before = path.read_text(encoding="utf-8")
        # 让 os.replace 抛错，验证旧文件保留
        import app.services.inference.feature.store as store_mod
        monkeypatch.setattr(store_mod.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(OSError):
            ledger.replace_segments(1, 1, "p", [_seg(source="p", producer="p", start=2, end=3)])
        assert path.read_text(encoding="utf-8") == before


# ============================ 配置 + 工厂 ============================

def _config(offline):
    return InferenceConfig({"stages": {"2": {
        "detectors": [{"name": "clean_large"}, {"name": "clean_small"}],
        "offline": offline,
    }}})


_OFFLINE_OK = {
    "enabled": True,
    "name": "clean_seg",
    "subscribes": ["clean_large", "clean_small"],
    "class": "app.services.inference.offline.segmenters.clean_action.CleanActionSegmenter",
    "params": {"label": "brushing"},
}


class TestCreateOfflineSegmenter:
    def test_disabled_variants_return_none(self):
        for offline in ({}, {"enabled": False}):
            seg = StageFactory(_config(offline)).create_offline_segmenter("2")
            assert seg is None

    def test_enabled_builds_segmenter(self):
        seg = StageFactory(_config(_OFFLINE_OK)).create_offline_segmenter("2")
        assert isinstance(seg, CleanActionSegmenter)
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
            "2", override_class="app.services.inference.offline.segmenters.clean_action.CleanActionSegmenter"
        )
        assert isinstance(seg, CleanActionSegmenter)


# ============================ 占位策略 ============================

class TestCleanActionSegmenter:
    def test_presence_runs_to_segments(self):
        seg = CleanActionSegmenter(name="p", subscribes=["a"])
        streams = {"a": [
            make_frame_detections(n=1, ts=1.0),   # active
            make_frame_detections(n=1, ts=2.0),   # active
            make_frame_detections(n=0, ts=3.0),   # idle → 断段
            make_frame_detections(n=1, ts=4.0),   # active（新段）
        ]}
        segs = seg.segment(seg.preprocess(streams))
        assert [(s.start, s.end) for s in segs] == [(1.0, 2.0), (4.0, 4.0)]
        assert all(s.source == "p" for s in segs)

    def test_min_frames_drops_short_runs(self):
        seg = CleanActionSegmenter(name="p", subscribes=["a"], min_frames=2)
        streams = {"a": [
            make_frame_detections(n=1, ts=1.0),   # 单帧段，min_frames=2 丢弃
            make_frame_detections(n=0, ts=2.0),
        ]}
        assert seg.segment(seg.preprocess(streams)) == []


# ============================ Runner ============================

def _runner(tmp_path, offline):
    return OfflineRunner(base_dir=tmp_path, config=_config(offline))


def _write_features(tmp_path, task_id, step_id):
    store = FeatureStore(tmp_path)
    _append_frame(store, task_id, step_id, 1.0, {
        "clean_large": make_frame_detections(n=1, ts=1.0),
        "clean_small": make_frame_detections(n=1, ts=1.0),
    })
    _append_frame(store, task_id, step_id, 2.0, {
        "clean_large": make_frame_detections(n=1, ts=2.0),
        "clean_small": make_frame_detections(n=1, ts=2.0),
    })
    store.flush(task_id, step_id)


class TestOfflineRunner:
    def test_unknown_stage_skipped(self, tmp_path):
        r = OfflineRunner(base_dir=tmp_path, config=_config(_OFFLINE_OK))
        res = r.run(OfflineRunSpec(task_id=1, step_id=999))
        assert res.status == "skipped"

    def test_offline_disabled_skipped(self, tmp_path):
        res = _runner(tmp_path, {}).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "skipped"

    def test_missing_input_skipped_no_write(self, tmp_path):
        res = _runner(tmp_path, _OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "skipped"
        assert not (tmp_path / "1" / "2" / "facts.jsonl").exists()

    def test_completed_writes_facts(self, tmp_path):
        _write_features(tmp_path, 1, 2)
        res = _runner(tmp_path, _OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "completed"
        assert res.producer == "clean_seg"
        assert res.segment_count == 1
        segs = [f for f in FactLedger(tmp_path).load(1, 2) if isinstance(f, SegmentFact)]
        assert len(segs) == 1
        assert segs[0].meta["producer"] == "clean_seg"
        assert segs[0].label == "brushing"

    def test_rerun_idempotent(self, tmp_path):
        _write_features(tmp_path, 1, 2)
        r = _runner(tmp_path, _OFFLINE_OK)
        r.run(OfflineRunSpec(task_id=1, step_id=2))
        r.run(OfflineRunSpec(task_id=1, step_id=2))
        segs = [f for f in FactLedger(tmp_path).load(1, 2) if isinstance(f, SegmentFact)]
        assert len(segs) == 1

    def test_strategy_exception_propagates_no_write(self, tmp_path):
        _write_features(tmp_path, 1, 2)
        r = OfflineRunner(base_dir=tmp_path, config=_config(dict(_OFFLINE_OK, params={})))
        with pytest.raises(RuntimeError):
            r.run(OfflineRunSpec(task_id=1, step_id=2,
                                 strategy="test_offline_pipeline.BoomSegmenter"))
        assert not (tmp_path / "1" / "2" / "facts.jsonl").exists()

    def test_preprocess_seam_invoked(self, tmp_path):
        _write_features(tmp_path, 1, 2)
        r = OfflineRunner(base_dir=tmp_path, config=_config(dict(_OFFLINE_OK, params={})))
        res = r.run(OfflineRunSpec(task_id=1, step_id=2,
                                   strategy="test_offline_pipeline.MarkerSegmenter"))
        assert res.status == "completed"
        assert res.segment_count == 1


class BoomSegmenter(OfflineSegmenter):
    def segment(self, model_input):
        raise RuntimeError("boom")


class MarkerSegmenter(OfflineSegmenter):
    """验证 preprocess 预留层被 runner 调用：preprocess 打标，segment 据标产段。"""

    def preprocess(self, streams):
        return {"marked": True, "streams": streams}

    def segment(self, model_input):
        assert model_input.get("marked") is True  # runner 确实先调了 preprocess
        return [SegmentFact(source=self.name, label="m", start=0.0, end=1.0)]


# ============================ CLI ============================

class TestCli:
    def test_completed_exit_zero(self, tmp_storage, monkeypatch, capsys):
        # 默认路径：OfflineRunner() 用 settings.storage_base_dir（tmp_storage 已指临时目录）
        # + runner 内 load_stage_config（monkeypatch 成临时 config，绕开单例）。
        from app.services.inference.offline import runner as runner_mod
        _write_features(tmp_storage, 1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["--task-id", "1", "--step-id", "2"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "completed" in out

    def test_error_exit_nonzero(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import runner as runner_mod
        _write_features(tmp_storage, 1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["--task-id", "1", "--step-id", "2",
                       "--strategy", "test_offline_pipeline.BoomSegmenter"])
        assert rc == 1
        assert "error" in capsys.readouterr().out

    def test_no_online_imports(self):
        """入口模块不得拉起在线服务模块。"""
        import importlib
        import sys
        for m in ("app.services.inference.manager", "app.main"):
            sys.modules.pop(m, None)
        importlib.import_module("app.services.inference.offline.cli")
        assert "app.main" not in sys.modules
