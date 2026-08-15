"""离线链路测试：存储引擎 / 配置工厂 / blocks 工具 / Segmenter / 编排 / CLI。

不依赖 GPU / RTSP / DB / 网络；storage、offline 缓存与 config 全用临时件，用例间不串。

结构对应生产分层：blocks 是工具（取块、缓存、回收），Segmenter 自取块出事实，
runner 是编排（它不认识块），cli 只管参数与进程。
"""

import argparse
import json

import pytest

from factories import make_detection, make_frame_detections, make_frame_feature
from offline_mock_segmenter import BrushRulesSegmenter

from app.domain.detection import FrameDetections
from app.services.inference.config import InferenceConfig
from app.services.inference.feature.store import FactLedger, FeatureStore
from app.services.inference.models import EventFact, SegmentFact
from app.services.inference.offline import blocks
from app.services.inference.offline.blocks import BlockKind, NoFeatures
from app.services.inference.offline.infer.segmenter import OfflineSegmenter
from app.services.inference.stage_factory import StageFactory

_MOCK_CLASS = "offline_mock_segmenter.BrushRulesSegmenter"
_CLEAN_CLASS = "app.services.inference.offline.infer.impl.clean.CleanMSTCNBiLSTMSegmenter"


# ============================ 存储引擎 ============================

def _append_frame(store: FeatureStore, task_id, step_id, ts, detectors,
                  frame_width=None, frame_height=None):
    """经 FeatureStore.append 写一帧（detectors: name -> FrameDetections）。"""
    store.append(task_id, step_id, make_frame_feature(
        ts=ts, by_source=detectors, frame_width=frame_width, frame_height=frame_height))


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
        """append 带帧级分辨率的帧 → load 还原到 FrameFeature.frame_width/height。"""
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
        segs = [f for f in ledger.load(1, 1) if isinstance(f, SegmentFact)]
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
        assert [f for f in ledger.load(1, 1) if isinstance(f, SegmentFact)] == []

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
            assert StageFactory(_config(offline)).create_offline_segmenter("2") is None

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

    def test_config_has_only_class_knob(self):
        """特征方案与网络由 checkpoint 绑死 → 配置里没有 net/feature 之类的第二个旋钮。

        换模型就换 Segmenter 类。这条钉住的是设计决定，不是实现细节。
        """
        from app.services.inference.offline.infer.impl import clean

        seg = clean.CleanBiGRUSegmenter(name="b", subscribes=["clean_large"])
        assert seg.needs == (BlockKind.BBOX,)
        assert seg.backbone is None
        assert not hasattr(seg, "net")


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


# ============================ blocks 工具层 ============================

def _clean_frame(ts):
    """一帧：clean_large=[hand, scope_control_body]，clean_small=[syringe]。

    只用**部署检测器真会产出**的类别（见 blocks/bbox.py OBJECTS）：刷具类基本检不出、
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


def _write_clean_features(base, task_id=1, step_id=2, ts_list=(0.1, 0.2, 0.3, 0.4)):
    store = FeatureStore(base)
    for t in ts_list:
        store.append(task_id, step_id, make_frame_feature(ts=t, by_source=_clean_frame(t)))
    store.flush(task_id, step_id)


def _write_features(base, task_id, step_id):
    store = FeatureStore(base)
    for ts in (1.0, 2.0):
        _append_frame(store, task_id, step_id, ts, {
            "clean_large": make_frame_detections(n=1, ts=ts),
            "clean_small": make_frame_detections(n=1, ts=ts),
        })
    store.flush(task_id, step_id)


class TestBlocksLoad:
    def test_bbox_block_shape_and_identity(self, tmp_path):
        _write_clean_features(tmp_path)
        blk = blocks.load(BlockKind.BBOX, 1, 2, storage_dir=tmp_path)
        assert blk.frame_count == 4
        assert blk.feature_dim == 71  # v3: hand top-2 + top-1/impute/relations + 时间编码
        assert blk.version == "clean_bbox_v3_detectable"
        assert blk.names[0] == "hand_count" and len(blk.names) == 71
        assert blk.spans == {"bbox": [0, 71]}
        assert blk.valid is None  # bbox 恒有效

    def test_missing_features_raises_no_features(self, tmp_path):
        with pytest.raises(NoFeatures):
            blocks.load(BlockKind.BBOX, 1, 2, storage_dir=tmp_path)

    def test_missing_subscribed_source_raises_no_features(self, tmp_path):
        _write_clean_features(tmp_path)
        with pytest.raises(NoFeatures, match="订阅 source"):
            blocks.load(BlockKind.BBOX, 1, 2, sources=["clean_large", "ghost"],
                        storage_dir=tmp_path)

    def test_visual_block_requires_backbone(self, tmp_path):
        _write_clean_features(tmp_path)
        with pytest.raises(ValueError, match="backbone"):
            blocks.load(BlockKind.VGLOBAL, 1, 2, storage_dir=tmp_path)

    def test_unimplemented_kind_fails(self, tmp_path):
        _write_clean_features(tmp_path)
        with pytest.raises(ValueError, match="尚未实现"):
            blocks.load(BlockKind.VHAND, 1, 2, storage_dir=tmp_path)

    def test_hand_slots_scale_with_box_count_not_step_length(self, tmp_path):
        """同一帧塞多只手不该让特征退化成 O(T × 框数)——槽位只看本帧候选。

        钉住 D6 修复：早先每个检测框都分配一条全长 [T,5] 稀疏数组，一条 step 的
        hand 有几千个框就是几千条全长数组（实测 1886 帧 6.3 s / 约 100 MB）。
        """
        store = FeatureStore(tmp_path)
        for t in (1.0, 2.0):
            store.append(1, 2, make_frame_feature(ts=t, by_source={"clean_large": FrameDetections(
                detections=[make_detection(class_name="hand", confidence=c) for c in (0.9, 0.8, 0.7)],
                metadata={}, timestamp=t,
            )}))
        store.flush(1, 2)
        blk = blocks.load(BlockKind.BBOX, 1, 2, storage_dir=tmp_path)
        idx = blk.names.index("hand_count")
        assert blk.values[0][idx] == pytest.approx(1.0)  # 3 只手 → clip(3,0,3)/3
        assert blk.values[:, blk.names.index("hand_top1_present")].tolist() == [1.0, 1.0]
        assert blk.values[:, blk.names.index("hand_top2_present")].tolist() == [1.0, 1.0]


class TestCache:
    def test_block_round_trip(self, tmp_path):
        from app.services.inference.offline.blocks import cache
        from app.services.inference.offline.models import FeatureBlock
        import numpy as np

        blk = FeatureBlock(
            values=np.arange(6, dtype=np.float32).reshape(3, 2),
            names=["a", "b"], ts=[1.0, 2.0, 3.0],
            valid=np.array([True, False, True]), version="v1", spans={"x": [0, 2]},
        )
        path = tmp_path / "blk.npz"
        cache.write_block(path, blk, extra={"pixel_hit": 2})
        back, extra = cache.read_block(path)
        assert np.array_equal(back.values, blk.values)
        assert back.names == blk.names and back.ts == blk.ts
        assert np.array_equal(back.valid, blk.valid)
        assert back.version == "v1" and back.spans == {"x": [0, 2]}
        assert extra == {"pixel_hit": 2}

    def test_corrupt_cache_reads_as_miss(self, tmp_path):
        from app.services.inference.offline.blocks import cache
        path = tmp_path / "blk.npz"
        path.write_bytes(b"not an npz")
        assert cache.read_block(path) is None

    def test_sweep_removes_expired_only(self, tmp_path):
        import os
        import time
        from app.services.inference.offline.blocks import cache

        d = cache.cache_dir(1, 2, tmp_path)
        d.mkdir(parents=True)
        fresh, stale = d / "fresh.npz", d / "stale.npz"
        fresh.write_bytes(b"x" * 10)
        stale.write_bytes(b"x" * 20)
        old = time.time() - 40 * 86400
        os.utime(stale, (old, old))
        freed = cache.sweep(ttl_days=30, base_dir=tmp_path, storage_dir=tmp_path / "nope")
        assert freed == 20
        assert fresh.exists() and not stale.exists()

    def test_sweep_clears_stray_playlists(self, tmp_path):
        """FrameSource 的临时 m3u8 在进程被 kill 时不会被 finally 清掉，靠 gc 收。"""
        from app.services.inference.offline.blocks import cache

        step_dir = tmp_path / "1" / "2"
        step_dir.mkdir(parents=True)
        stray = step_dir / ".export_abcd.m3u8"
        stray.write_text("#EXTM3U")
        cache.sweep(ttl_days=30, base_dir=tmp_path / "offline", storage_dir=tmp_path)
        assert not stray.exists()


# ============================ Segmenter ============================

class TestBrushRulesSegmenter:
    def test_presence_runs_to_segments(self, tmp_path):
        store = FeatureStore(tmp_path)
        for ts, n in ((1.0, 1), (2.0, 1), (3.0, 0), (4.0, 1)):
            _append_frame(store, 1, 2, ts, {"a": make_frame_detections(n=n, ts=ts)})
        store.flush(1, 2)
        seg = BrushRulesSegmenter(name="p", subscribes=["a"], storage_dir=tmp_path)
        segs = seg.segment(1, 2)
        assert [(s.start, s.end) for s in segs] == [(1.0, 2.0), (4.0, 4.0)]
        assert all(s.source == "p" for s in segs)

    def test_min_frames_drops_short_runs(self, tmp_path):
        store = FeatureStore(tmp_path)
        for ts, n in ((1.0, 1), (2.0, 0)):
            _append_frame(store, 1, 2, ts, {"a": make_frame_detections(n=n, ts=ts)})
        store.flush(1, 2)
        seg = BrushRulesSegmenter(name="p", subscribes=["a"], min_frames=2, storage_dir=tmp_path)
        assert seg.segment(1, 2) == []

    def test_debug_result_none(self, tmp_path):
        """presence 型无逐帧语义：debug_result 恒 None（cli 据此不落逐帧产物）。"""
        store = FeatureStore(tmp_path)
        _append_frame(store, 1, 2, 1.0, {"a": make_frame_detections(n=1, ts=1.0)})
        store.flush(1, 2)
        seg = BrushRulesSegmenter(name="p", subscribes=["a"], storage_dir=tmp_path)
        seg.segment(1, 2)
        assert seg.debug_result() is None


class TestCleanSegmenter:
    """三个 clean Segmenter 各吃自己那套特征——一个 checkpoint 一个类，不是自由组合。"""

    def test_each_model_builds_its_own_input(self, tmp_path):
        from app.services.inference.offline.infer.impl import clean

        _write_clean_features(tmp_path)
        bl = {BlockKind.BBOX: blocks.load(BlockKind.BBOX, 1, 2, storage_dir=tmp_path)}
        cases = [
            (clean.CleanMSTCNBiLSTMSegmenter, "v3", 71, "clean_bbox_v3_detectable"),
            (clean.CleanASFormerSegmenter, "business_priors", 73,
             "clean_bbox_v3_detectable+business_priors"),
            (clean.CleanBiGRUSegmenter, "window_stats+business_priors", 151,
             "clean_bbox_v3_detectable+center_window+business_priors"),
        ]
        for cls, method, dim, version in cases:
            seg = cls(name="s", subscribes=["clean_large", "clean_small"], storage_dir=tmp_path)
            mi = seg.build_input(bl)
            assert (seg.feature_method, mi.feature_dim, mi.version) == (method, dim, version)
            assert mi.frame_count == 4
            assert len(mi.names) == dim
            import numpy as np
            assert np.isfinite(mi.values).all()

    def test_no_model_path_hard_fails_without_debug_result(self, tmp_path):
        from app.services.inference.offline.infer.impl import clean

        _write_clean_features(tmp_path)
        seg = clean.CleanMSTCNBiLSTMSegmenter(
            name="clean_seg", subscribes=["clean_large", "clean_small"],
            min_duration_s=0.1, storage_dir=tmp_path,
        )
        with pytest.raises(ValueError, match="model_path"):
            seg.segment(1, 2)
        assert seg.debug_result() is None

    def test_missing_input_raises_no_features(self, tmp_path):
        from app.services.inference.offline.infer.impl import clean

        seg = clean.CleanMSTCNBiLSTMSegmenter(
            name="clean_seg", subscribes=["clean_large"], storage_dir=tmp_path)
        with pytest.raises(NoFeatures):
            seg.segment(1, 2)


# ============================ 编排（runner.run_infer） ============================

def _args(task_id=1, step_id=2, offline=None, segmenter=None, device="cpu"):
    return argparse.Namespace(
        task_id=task_id, step_id=step_id, segmenter=segmenter, device=device,
        threads=1, cache_ttl_days=30, out_dir=None, config=_config(offline),
    )


class TestRunInfer:
    def test_unknown_stage_skipped(self, tmp_path):
        from app.services.inference.offline import runner
        res = runner.run_infer(_args(step_id=999, offline=_OFFLINE_OK), storage_dir=tmp_path)
        assert res.status == "skipped"

    def test_offline_disabled_skipped(self, tmp_path):
        from app.services.inference.offline import runner
        res = runner.run_infer(_args(offline={}), storage_dir=tmp_path)
        assert res.status == "skipped"

    def test_missing_input_skipped_no_write(self, tmp_path):
        from app.services.inference.offline import runner
        res = runner.run_infer(_args(offline=_OFFLINE_OK), storage_dir=tmp_path)
        assert res.status == "skipped"
        assert not (tmp_path / "1" / "2" / "facts.jsonl").exists()

    def test_completed_writes_facts(self, tmp_path):
        from app.services.inference.offline import runner
        _write_features(tmp_path, 1, 2)
        res = runner.run_infer(_args(offline=_OFFLINE_OK), storage_dir=tmp_path)
        assert (res.status, res.producer, res.segment_count) == ("completed", "clean_seg", 1)
        segs = [f for f in FactLedger(tmp_path).load(1, 2) if isinstance(f, SegmentFact)]
        assert len(segs) == 1
        assert segs[0].meta["producer"] == "clean_seg"
        assert segs[0].label == "brushing"

    def test_rerun_idempotent(self, tmp_path):
        from app.services.inference.offline import runner
        _write_features(tmp_path, 1, 2)
        for _ in range(2):
            runner.run_infer(_args(offline=_OFFLINE_OK), storage_dir=tmp_path)
        segs = [f for f in FactLedger(tmp_path).load(1, 2) if isinstance(f, SegmentFact)]
        assert len(segs) == 1

    def test_strategy_exception_propagates_no_write(self, tmp_path):
        from app.services.inference.offline import runner
        _write_features(tmp_path, 1, 2)
        args = _args(offline=dict(_OFFLINE_OK, params={}),
                     segmenter="test_offline_pipeline.BoomSegmenter")
        with pytest.raises(RuntimeError):
            runner.run_infer(args, storage_dir=tmp_path)
        assert not (tmp_path / "1" / "2" / "facts.jsonl").exists()

    def test_debug_result_lands_in_cache_not_step_dir(self, tmp_path):
        """逐帧产物是可重建的调试产物 → 落 .cache，不落受 TTL 的 step 目录。"""
        from app.services.inference.offline import runner
        _write_features(tmp_path, 1, 2)
        offline_dir = tmp_path / "offline"
        args = _args(offline=dict(_OFFLINE_OK, params={}),
                     segmenter="test_offline_pipeline.DebugSegmenter")
        res = runner.run_infer(args, storage_dir=tmp_path, offline_dir=offline_dir)
        assert res.status == "completed"
        assert not (tmp_path / "1" / "2" / "offline_inference_result.json").exists()
        assert (offline_dir / ".cache" / "1" / "2" / "infer_DebugSegmenter.json").exists()

    def test_resolve_stage_fallback_to_mock(self, tmp_path):
        """未配数字 step_id(-1) 经 resolve_stage 回退 MOCK.offline，读数字 -1 分区、completed。"""
        from app.services.inference.offline import runner
        cfg = InferenceConfig({"stages": {"MOCK": {
            "detectors": [{"name": "mock"}],
            "offline": {"name": "mock_offline", "subscribes": ["mock"],
                        "class": _MOCK_CLASS, "params": {"label": "mock_action", "min_frames": 1}},
        }}})
        store = FeatureStore(tmp_path)
        # MockDetector 纯透传：空检测帧 → 0 段，但链路走通
        store.append(1, -1, make_frame_feature(
            ts=1.0, by_source={"mock": make_frame_detections(n=0, ts=1.0)}))
        store.flush(1, -1)
        args = _args(step_id=-1, offline={})
        args.config = cfg
        res = runner.run_infer(args, storage_dir=tmp_path)
        assert (res.status, res.producer, res.segment_count) == ("completed", "mock_offline", 0)

    def test_orchestration_does_not_touch_blocks(self):
        """编排层不认识块：runner 里不出现 BlockKind / blocks.load 这类块词汇。

        取块是 Segmenter 的事，这条钉住分层——编排只碰 NoFeatures（无输入 → skipped）。
        """
        import inspect
        from app.services.inference.offline import runner
        src = inspect.getsource(runner.run_infer)
        assert "BlockKind" not in src
        assert "blocks.load" not in src


class BoomSegmenter(OfflineSegmenter):
    def segment(self, task_id, step_id):
        raise RuntimeError("boom")


class DebugSegmenter(OfflineSegmenter):
    """产逐帧调试产物的最小策略：验证 cli 把它落进 .cache。"""

    def segment(self, task_id, step_id):
        return [SegmentFact(source=self.name, label="m", start=0.0, end=1.0)]

    def debug_result(self):
        return {"frame_predictions": [{"ts": 0.0, "label": "m", "conf": 1.0}]}


# ============================ 导出（runner.run_export） ============================

class TestRunExport:
    def test_export_writes_npz_and_manifest(self, tmp_path):
        from app.services.inference.offline import runner
        import numpy as np

        _write_clean_features(tmp_path)
        offline_dir = tmp_path / "offline"
        args = _args(offline=dict(_OFFLINE_OK, **{"class": _CLEAN_CLASS, "params": {}}))
        res = runner.run_export(args, storage_dir=tmp_path, offline_dir=offline_dir)
        assert res.status == "completed"

        d = offline_dir / ".cache" / "1" / "2"
        tag = "CleanMSTCNBiLSTMSegmenter"
        data = np.load(d / f"input_{tag}.npz")
        assert data["features"].shape == (4, 71)
        manifest = json.loads((d / f"manifest_{tag}.json").read_text(encoding="utf-8"))
        assert manifest["feature_dim"] == 71
        assert manifest["feature_version"] == "clean_bbox_v3_detectable"
        assert manifest["spans"] == {"bbox": [0, 71]}
        assert manifest["backbone"] == "none"
        assert len(manifest["feature_names"]) == 71

    def test_export_and_infer_share_one_build_input(self, tmp_path):
        """导出的字节 == 推理实际吃的字节：两条路径调同一个 build_input。"""
        from app.services.inference.offline import runner
        from app.services.inference.offline.infer.impl import clean
        import numpy as np

        _write_clean_features(tmp_path)
        offline_dir = tmp_path / "offline"
        args = _args(offline=dict(_OFFLINE_OK, **{"class": _CLEAN_CLASS, "params": {}}))
        runner.run_export(args, storage_dir=tmp_path, offline_dir=offline_dir)
        exported = np.load(
            offline_dir / ".cache" / "1" / "2" / "input_CleanMSTCNBiLSTMSegmenter.npz"
        )["features"]

        seg = clean.CleanMSTCNBiLSTMSegmenter(
            name="clean_seg", subscribes=["clean_large", "clean_small"], storage_dir=tmp_path)
        inferred = seg.build_input(seg.load_blocks(1, 2)).values
        assert np.array_equal(exported, inferred)

    def test_export_missing_input_skipped(self, tmp_path):
        from app.services.inference.offline import runner
        args = _args(offline=dict(_OFFLINE_OK, **{"class": _CLEAN_CLASS, "params": {}}))
        res = runner.run_export(args, storage_dir=tmp_path, offline_dir=tmp_path / "offline")
        assert res.status == "skipped"


# ============================ CLI ============================

class TestCli:
    def test_infer_completed_exit_zero(self, tmp_storage, tmp_offline, monkeypatch, capsys):
        from app.services.inference.offline import cli
        _write_features(tmp_storage, 1, 2)
        monkeypatch.setattr(cli, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK),
                            raising=False)
        monkeypatch.setattr(
            "app.services.inference.config.load_stage_config",
            lambda *a, **k: _config(_OFFLINE_OK),
        )
        rc = cli.main(["infer", "--task-id", "1", "--step-id", "2"])
        assert rc == 0
        assert "completed" in capsys.readouterr().out

    def test_infer_error_exit_nonzero(self, tmp_storage, tmp_offline, monkeypatch, capsys):
        from app.services.inference.offline import cli
        _write_features(tmp_storage, 1, 2)
        monkeypatch.setattr(
            "app.services.inference.config.load_stage_config",
            lambda *a, **k: _config(_OFFLINE_OK),
        )
        rc = cli.main(["infer", "--task-id", "1", "--step-id", "2",
                       "--segmenter", "test_offline_pipeline.BoomSegmenter"])
        assert rc == 1
        assert "error" in capsys.readouterr().out

    def test_query_roundtrip(self, tmp_storage, tmp_offline, monkeypatch, capsys):
        """infer 写出 facts 后，query 子命令能读回时间线。"""
        from app.services.inference.offline import cli
        _write_features(tmp_storage, 1, 2)
        monkeypatch.setattr(
            "app.services.inference.config.load_stage_config",
            lambda *a, **k: _config(_OFFLINE_OK),
        )
        assert cli.main(["infer", "--task-id", "1", "--step-id", "2"]) == 0
        capsys.readouterr()  # 清 infer 的输出
        assert cli.main(["query", "--task-id", "1", "--step-id", "2"]) == 0
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
