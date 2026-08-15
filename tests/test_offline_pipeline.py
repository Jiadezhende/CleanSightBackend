"""离线分割入口测试：存储引擎 / 配置工厂 / Runner / mock+clean 策略 / stage 解析 / CLI。

不依赖 GPU / RTSP / DB / 网络；storage 与 config 全用临时件，用例间不串。
"""

import json
import math

import pytest

from factories import make_detection, make_frame_detections, make_frame_feature

from app.domain.detection import FrameDetections, FrameFeature
from app.services.inference.config import InferenceConfig
from app.services.inference.feature.store import FactLedger, FeatureStore
from app.services.inference.models import EventFact, SegmentFact
from app.services.inference.offline.segmenter import OfflineSegmenter
from app.services.inference.offline.runner import OfflineRunner, OfflineRunSpec
from app.services.inference.offline.impl.mock import BrushRulesSegmenter
from app.services.inference.stage_factory import StageFactory

_MOCK_CLASS = "app.services.inference.offline.impl.mock.BrushRulesSegmenter"
_CLEAN_CLASS = "app.services.inference.offline.impl.clean.CleanSegmenter"


# ============================ 存储引擎 ============================

def _append_frame(store: FeatureStore, task_id, step_id, ts, detectors,
                  frame_width=None, frame_height=None):
    """经 FeatureStore.append 写一帧（detectors: name -> FrameDetections）。frame_width/height 为帧级分辨率。"""
    store.append(task_id, step_id, make_frame_feature(
        ts=ts, by_source=detectors, frame_width=frame_width, frame_height=frame_height))


def _frames(per_source):
    """{src: [FrameDetections 按 ts]} → List[FrameFeature]（按 ts 对齐+升序），供直调 preprocess。"""
    by_ts: dict = {}
    for src, fds in per_source.items():
        for fd in fds:
            by_ts.setdefault(fd.timestamp, {})[src] = fd
    return [FrameFeature(ts=ts, by_source=by_ts[ts]) for ts in sorted(by_ts)]


class TestLoad:
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
        frames = store.load(7, 2)
        assert [ff.ts for ff in frames] == [1.0, 2.0, 3.0]  # 按 ts 升序
        # by_source 键集 = 该帧含的 source（present-key）；空检测帧保留
        assert set(frames[0].by_source) == {"clean_large", "clean_small"}
        assert set(frames[1].by_source) == {"clean_large", "clean_small"}
        assert set(frames[2].by_source) == {"clean_large"}   # ts=3 只有 large
        assert len(frames[1].by_source["clean_large"].detections) == 0  # 空检测帧保留

    def test_missing_file_returns_empty(self, tmp_path):
        assert FeatureStore(tmp_path).load(1, 1) == []

    def test_sorted_by_ts(self, tmp_path):
        store = FeatureStore(tmp_path)
        for ts in (3.0, 1.0, 2.0):
            _append_frame(store, 1, 1, ts, {"a": make_frame_detections(n=1, ts=ts)})
        assert [ff.ts for ff in store.load(1, 1)] == [1.0, 2.0, 3.0]

    def test_corrupt_line_skipped(self, tmp_path):
        store = FeatureStore(tmp_path)
        _append_frame(store, 1, 1, 1.0, {"a": make_frame_detections(n=1, ts=1.0)})
        store.flush(1, 1)
        path = tmp_path / "1" / "1" / "features.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write("{ not json\n")
        _append_frame(store, 1, 1, 2.0, {"a": make_frame_detections(n=1, ts=2.0)})
        assert [ff.ts for ff in store.load(1, 1)] == [1.0, 2.0]

    def test_wh_round_trip_restores_frame_size(self, tmp_path):
        """append 带帧级分辨率的帧 → load 还原到 FrameFeature.frame_width/height（不再灌 metadata）。"""
        store = FeatureStore(tmp_path)
        _append_frame(store, 1, 1, 1.0, {
            "a": make_frame_detections(n=1, ts=1.0),
        }, frame_width=640, frame_height=480)
        ff = store.load(1, 1)[0]
        assert (ff.frame_width, ff.frame_height) == (640, 480)
        assert ff.by_source["a"].metadata == {}
        assert len(ff.by_source["a"].detections) == 1

    def test_utf8_bom_tolerated(self, tmp_path):
        """Windows 手写 features.jsonl 的 UTF-8 BOM 应能被 load 正常还原。"""
        path = tmp_path / "1" / "1" / "features.jsonl"
        path.parent.mkdir(parents=True)
        row = {"ts": 1.0, "features": {"a": [{"bbox": [1, 2, 3, 4], "conf": 0.9, "cls_id": 0, "cls": "hand"}]}}
        path.write_text("﻿" + json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        frames = FeatureStore(tmp_path).load(1, 1)
        assert [ff.ts for ff in frames] == [1.0]
        assert len(frames[0].by_source["a"].detections) == 1


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
            make_frame_detections(n=1, ts=1.0),   # active
            make_frame_detections(n=1, ts=2.0),   # active
            make_frame_detections(n=0, ts=3.0),   # idle → 断段
            make_frame_detections(n=1, ts=4.0),   # active（新段）
        ]}
        segs = seg.segment(seg.preprocess(_frames(streams)))
        assert [(s.start, s.end) for s in segs] == [(1.0, 2.0), (4.0, 4.0)]
        assert all(s.source == "p" for s in segs)

    def test_min_frames_drops_short_runs(self):
        seg = BrushRulesSegmenter(name="p", subscribes=["a"], min_frames=2)
        streams = {"a": [
            make_frame_detections(n=1, ts=1.0),   # 单帧段，min_frames=2 丢弃
            make_frame_detections(n=0, ts=2.0),
        ]}
        assert seg.segment(seg.preprocess(_frames(streams))) == []

    def test_debug_result_none(self):
        """presence 型无逐帧语义：debug_result 恒 None（Runner 据此不落逐帧 JSON）。"""
        seg = BrushRulesSegmenter(name="p", subscribes=["a"])
        seg.segment(seg.preprocess(_frames({"a": [make_frame_detections(n=1, ts=1.0)]})))
        assert seg.debug_result() is None


# ============================ CleanSegmenter（CLEAN baseline） ============================

def _clean_frame(ts):
    """一帧：clean_large=[hand, scope_control_body]，clean_small=[syringe]。

    只用**部署检测器真会产出**的类别（见 impl/clean.py OBJECTS）：刷具类基本检不出、
    已不在特征输入内，用它造帧只会得到一堆恒零列，测不出任何东西。
    """
    large = FrameDetections(
        detections=[make_detection(class_name="hand"),
                    make_detection(class_name="scope_control_body")],
        metadata={}, timestamp=ts,
    )
    small = FrameDetections(
        detections=[make_detection(class_name="syringe")], metadata={}, timestamp=ts,
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
        assert mi.frame_count == 4 and mi.feature_dim == 71  # v3: hand top-2 + top-1/impute/relations
        assert mi.feature_version == "clean_bbox_v3_detectable"
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
            "v3", 71, "clean_bbox_v3_detectable",
        )
        assert (asformer.feature_method, asformer_input.feature_dim, asformer_input.feature_version) == (
            "business_priors", 73, "clean_bbox_v3_detectable+business_priors",
        )
        assert (bigru.feature_method, bigru_input.feature_dim, bigru_input.feature_version) == (
            "window_stats+business_priors", 151, "clean_bbox_v3_detectable+center_window+business_priors",
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
        # BrushRulesSegmenter.debug_result() 为 None → 不落逐帧 JSON
        assert not (tmp_path / "1" / "2" / "offline_inference_result.json").exists()

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

    def test_clean_segmenter_without_model_path_fails_no_write(self, tmp_path):
        """CleanSegmenter 不再规则降级；未配 model_path 时硬失败且不落结果。"""
        store = FeatureStore(tmp_path)
        for t in (0.1, 0.2, 0.3, 0.4):
            store.append(1, 2, make_frame_feature(ts=t, by_source=_clean_frame(t)))
        store.flush(1, 2)
        offline = dict(_OFFLINE_OK, **{"class": _CLEAN_CLASS,
                                       "params": {"min_duration_s": 0.1, "fps": 10.0}})
        with pytest.raises(ValueError, match="model_path"):
            OfflineRunner(base_dir=tmp_path, config=_config(offline)).run(
                OfflineRunSpec(task_id=1, step_id=2))
        dbg_path = tmp_path / "1" / "2" / "offline_inference_result.json"
        assert not dbg_path.exists()
        assert not (tmp_path / "1" / "2" / "facts.jsonl").exists()

    def test_resolve_stage_fallback_to_mock(self, tmp_path):
        """未配数字 step_id(-1) 经 resolve_stage 回退 MOCK.offline，读数字 -1 分区、completed。"""
        cfg = InferenceConfig({"stages": {"MOCK": {
            "detectors": [{"name": "mock"}],
            "offline": {"name": "mock_offline", "subscribes": ["mock"],
                        "class": _MOCK_CLASS, "params": {"label": "mock_action", "min_frames": 1}},
        }}})
        store = FeatureStore(tmp_path)
        # MockDetector 纯透传：空检测帧 → 0 段，但链路走通
        store.append(1, -1, make_frame_feature(ts=1.0,
                                               by_source={"mock": make_frame_detections(n=0, ts=1.0)}))
        store.flush(1, -1)
        res = OfflineRunner(base_dir=tmp_path, config=cfg).run(OfflineRunSpec(task_id=1, step_id=-1))
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
        return [SegmentFact(source=self.name, label="m", start=0.0, end=1.0)]


# ============================ CLI ============================

class TestCli:
    def test_run_completed_exit_zero(self, tmp_storage, monkeypatch, capsys):
        # 默认路径：OfflineRunner() 用 settings.storage_base_dir（tmp_storage 已指临时目录）
        # + runner 内 load_stage_config（monkeypatch 成临时 config，绕开单例）。
        from app.services.inference.offline import runner as runner_mod
        _write_features(tmp_storage, 1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "completed" in out

    def test_run_error_exit_nonzero(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline import runner as runner_mod
        _write_features(tmp_storage, 1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2",
                       "--strategy", "test_offline_pipeline.BoomSegmenter"])
        assert rc == 1
        assert "error" in capsys.readouterr().out

    def test_query_roundtrip(self, tmp_storage, monkeypatch, capsys):
        """run 写出 facts 后，query 子命令能读回时间线。"""
        from app.services.inference.offline import runner as runner_mod
        _write_features(tmp_storage, 1, 2)
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
