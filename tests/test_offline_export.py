"""离线导出与视觉块测试：raw 帧索引 sidecar / 视觉块 / 特征健康诊断 / 导出 CLI 参数。

不依赖 GPU / ffmpeg / RTSP / DB：sidecar 的构造与解析是纯函数（`cv2.VideoWriter` 写段、
段解码属 I/O 边界，按开发规范留给集成测试）；导出链路全用临时 storage 与临时 offline 目录。

导出**本身**的行为（npz + manifest、与推理同源）在 test_offline_pipeline.py 里测，
那里离编排更近；本文件测的是它周边的输入（sidecar、视觉块）与输出（诊断）。
"""

import json
import os

import numpy as np
import pytest

from factories import make_detection, make_frame_detections, make_frame_feature

from app.domain.detection import FrameDetections
from app.domain.frame import Frame
from app.services.inference.feature.store import FeatureStore
from app.services.inference.offline import blocks
from app.services.inference.offline.blocks import BlockKind
from app.services.persistence.strategies.raw_frame_index import (
    build_frame_index,
    index_path_for,
    read_frame_index,
    write_frame_index,
)

_CLEAN_CLASS = "app.services.inference.offline.infer.impl.clean.CleanMSTCNBiLSTMSegmenter"


def _frames(*ts):
    """构造 Frame 序列（像素全零，只有 timestamp 参与索引）。"""
    return [Frame(timestamp=t, frame=np.zeros((2, 2, 3), dtype=np.uint8)) for t in ts]


def _write_features(base, task_id, step_id, ts_list, *, w=640, h=480):
    """经 FeatureStore 写若干帧 clean 特征（单一真源，不手写 jsonl）。"""
    store = FeatureStore(base)
    for ts in ts_list:
        store.append(task_id, step_id, make_frame_feature(
            ts=ts,
            by_source={"clean_large": make_frame_detections(n=1, class_name="hand", ts=ts)},
            frame_width=w, frame_height=h,
        ))
    store.flush(task_id, step_id)
    return store


# ============================ raw 帧索引 sidecar ============================

class TestRawFrameIndex:
    def test_build_keeps_order_and_only_frame_ts(self):
        """只记 frame_ts，顺序原样保留。

        刻意不记 eff_fps / 段时长 / timescale —— 那些容器层参数正被在制的 HLS 时基修复与
        墙钟时间轴两篇需求重写，记下来就是第二份会漂移的真源。
        """
        rec = build_frame_index(_frames(3.0, 1.0, 2.0))
        assert rec == {"frame_ts": [3.0, 1.0, 2.0]}  # 不排序：重排会破坏 ordinal 对应关系
        assert set(rec) == {"frame_ts"}

    def test_roundtrip(self, tmp_path):
        seg = tmp_path / "raw_segment_1785995202505123.mp4"
        ts = [1785995202.505123, 1785995202.571790, 1785995202.638457]
        assert write_frame_index(seg, _frames(*ts)) is True
        assert index_path_for(seg) == tmp_path / "raw_segment_1785995202505123.idx.json"
        assert read_frame_index(seg) == ts

    def test_ordinal_lookup_is_exact(self, tmp_path):
        """sidecar 存在时，ts → 段内 ordinal 精确可逆（不靠 eff_fps 反推）。"""
        seg = tmp_path / "raw_segment_1000000.mp4"
        ts = [1.0, 1.13, 1.19, 1.34, 1.51]  # 刻意非等间隔：抖动下近似反推会错
        write_frame_index(seg, _frames(*ts))
        frame_ts = read_frame_index(seg)
        for expected, t in enumerate(ts):
            assert frame_ts.index(t) == expected

    def test_missing_sidecar_returns_none(self, tmp_path):
        """无索引返回 None —— 调用方据此判为不可精确取帧，而非退化成近似反推。"""
        assert read_frame_index(tmp_path / "raw_segment_1.mp4") is None

    def test_corrupt_sidecar_returns_none(self, tmp_path):
        seg = tmp_path / "raw_segment_2.mp4"
        index_path_for(seg).write_text("{not json", encoding="utf-8")
        assert read_frame_index(seg) is None

    def test_empty_frames_writes_nothing(self, tmp_path):
        seg = tmp_path / "raw_segment_3.mp4"
        assert write_frame_index(seg, []) is False
        assert not index_path_for(seg).exists()

    def test_write_failure_is_best_effort(self, tmp_path):
        """落盘失败只告警不抛：落盘主链路可用性优先于实验数据完整性。"""
        missing_dir = tmp_path / "nonexistent" / "raw_segment_4.mp4"
        assert write_frame_index(missing_dir, _frames(1.0)) is False

    def test_no_tmp_file_left_behind(self, tmp_path):
        seg = tmp_path / "raw_segment_5.mp4"
        write_frame_index(seg, _frames(1.0, 2.0))
        assert [p.name for p in tmp_path.glob("*.tmp")] == []


# ============================ 视觉块 ============================

class TestVisualBlock:
    def test_no_pixels_available_fails_loudly(self, tmp_path):
        """没有任何 raw 段/sidecar 时，视觉分支硬失败并报出分项原因。

        绝不产出「全零视觉特征」的块——那会让训练侧以为拿到了视觉信息。
        （sidecar 落地之前录的 step 正是这种情况。）
        """
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        with pytest.raises(RuntimeError, match="没有取到任何像素帧"):
            blocks.load(BlockKind.VGLOBAL, 7, 2, backbone="yolo",
                        storage_dir=tmp_path, offline_dir=tmp_path / "offline")

    def test_visual_needs_backbone(self, tmp_path):
        """没有 backbone 就没有视觉块——不静默退化成 bbox-only（否则对照会变成自己比自己）。"""
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        with pytest.raises(ValueError, match="backbone"):
            blocks.load(BlockKind.VGLOBAL, 7, 2, storage_dir=tmp_path)

    def test_missing_frame_size_refuses_to_guess(self, tmp_path):
        """features 没记分辨率时视觉块硬失败：rawvideo 按尺寸切帧，猜错就是整段像素错位。"""
        store = FeatureStore(tmp_path)
        store.append(7, 2, make_frame_feature(
            ts=1.0, by_source={"clean_large": make_frame_detections(n=1, ts=1.0)}))
        store.flush(7, 2)
        with pytest.raises(ValueError, match="帧分辨率"):
            blocks.load(BlockKind.VGLOBAL, 7, 2, backbone="yolo", storage_dir=tmp_path)

    def test_cache_hit_skips_forward(self, tmp_path, monkeypatch):
        """视觉块命中缓存就不再解码/前向——这是缓存存在的唯一理由。"""
        from app.services.inference.offline.blocks import cache, visual
        from app.services.inference.offline.models import FeatureBlock

        offline_dir = tmp_path / "offline"
        blk = FeatureBlock(
            values=np.ones((2, 4), dtype=np.float32),
            names=[f"visual_global_{i}" for i in range(4)],
            ts=[1.0, 2.0], valid=np.array([True, False]),
            version="visual_global@yolo", spans={"vglobal": [0, 4]},
        )
        path = cache.cache_dir(7, 2, offline_dir) / "vglobal_yolo.npz"
        cache.write_block(path, blk, extra={"pixel_hit": 1, "no_sidecar": 1})

        def _boom(*a, **k):
            raise AssertionError("命中缓存后不该再前向")

        monkeypatch.setattr(visual, "_forward", _boom)
        stats = blocks.FetchStats()
        got = visual.build(7, 2, [1.0, 2.0], 640, 480, "yolo",
                           offline_dir=offline_dir, stats=stats)
        assert np.array_equal(got.values, blk.values)
        assert got.version == "visual_global@yolo"
        assert (stats.pixel_hit, stats.no_sidecar) == (1, 1)  # 统计由 npz 头回填

    def test_cache_miss_on_frame_count_change(self, tmp_path, monkeypatch):
        """帧数对不上的缓存视为未命中——宁可重算也不能拿错长度的块去对齐。"""
        from app.services.inference.offline.blocks import cache, visual
        from app.services.inference.offline.models import FeatureBlock

        offline_dir = tmp_path / "offline"
        blk = FeatureBlock(values=np.ones((2, 4), dtype=np.float32),
                           names=[f"visual_global_{i}" for i in range(4)], ts=[1.0, 2.0])
        cache.write_block(cache.cache_dir(7, 2, offline_dir) / "vglobal_yolo.npz", blk)

        called = {"n": 0}

        def _fake_forward(*a, **k):
            called["n"] += 1
            return blk, blocks.FetchStats()

        monkeypatch.setattr(visual, "_forward", _fake_forward)
        visual.build(7, 2, [1.0, 2.0, 3.0], 640, 480, "yolo", offline_dir=offline_dir)
        assert called["n"] == 1

    @pytest.mark.parametrize("spec,want", [
        ("yolo:clean-large-best.pt", "yolo-clean-large-best"),
        ("resnet18", "resnet18"),
        ("yolo:/abs/path/x.pt", "yolo--abs-path-x"),
    ])
    def test_backbone_sanitized_for_filename(self, spec, want):
        from app.services.inference.offline.blocks import visual
        assert visual.sanitize(spec) == want


# ============================ 特征健康诊断 ============================

class TestDiagnose:
    """无效特征检测：结构性无效（到处恒定）vs 数据相关无效（这条片段恰好没出现）。"""

    def _export(self, tmp_path, task_id, ts_list):
        import argparse
        from app.services.inference.config import InferenceConfig
        from app.services.inference.offline import runner

        _write_features(tmp_path, task_id, 2, ts_list)
        config = InferenceConfig({"stages": {"2": {
            "detectors": [{"name": "clean_large"}],
            "offline": {"name": "s", "subscribes": ["clean_large"],
                        "class": _CLEAN_CLASS, "params": {}},
        }}})
        args = argparse.Namespace(
            task_id=task_id, step_id=2, segmenter=None, device="cpu",
            threads=1, cache_ttl_days=30, out_dir=None, config=config,
        )
        return runner.run_export(args, storage_dir=tmp_path, offline_dir=tmp_path / "offline")

    def test_single_step_cannot_judge_structural(self, tmp_path):
        """**只有 1 个 step 时无法区分两类无效** —— 报告须显式标注这个局限。

        单条片段上的恒定列，既可能是契约声明了检不出的类别（结构性），也可能只是
        这段视频没出现该目标。不加警示就会把后者误删。
        """
        from app.services.inference.offline.diagnose import format_report, scan_columns

        self._export(tmp_path, 7, [1.0, 1.2, 1.4])
        reports = scan_columns(tmp_path / "offline" / ".cache")
        assert list(reports) == ["CleanMSTCNBiLSTMSegmenter"]
        assert len(reports["CleanMSTCNBiLSTMSegmenter"].steps) == 1
        assert "只有 1 个 step" in format_report(reports)

    def test_cross_step_separates_structural_from_data_dependent(self, tmp_path):
        """跨 step 聚合才有判定力：全 step 恒定 = 结构性可疑，部分恒定 = 数据相关。"""
        import argparse
        from app.services.inference.config import InferenceConfig
        from app.services.inference.offline import runner
        from app.services.inference.offline.diagnose import scan_columns

        # step A 只有 hand；step B 有 hand + syringe → syringe 列在 A 恒定、在 B 不恒定
        self._export(tmp_path, 7, [1.0, 1.2, 1.4])
        store = FeatureStore(tmp_path)
        for i, ts in enumerate([1.0, 1.2, 1.4]):
            dets = {"clean_large": make_frame_detections(n=1, class_name="hand", ts=ts)}
            if i:  # 后两帧才出现 syringe，且位置不同 → 该列有变化
                dets["clean_small"] = FrameDetections(
                    detections=[make_detection(class_name="syringe",
                                               bbox=[10 * i, 10 * i, 40 * i, 40 * i])],
                    metadata={}, timestamp=ts,
                )
            store.append(8, 2, make_frame_feature(
                ts=ts, by_source=dets, frame_width=640, frame_height=480))
        store.flush(8, 2)
        config = InferenceConfig({"stages": {"2": {
            "detectors": [{"name": "clean_large"}, {"name": "clean_small"}],
            "offline": {"name": "s", "subscribes": ["clean_large"],
                        "class": _CLEAN_CLASS, "params": {}},
        }}})
        runner.run_export(
            argparse.Namespace(task_id=8, step_id=2, segmenter=None, device="cpu",
                               threads=1, cache_ttl_days=30, out_dir=None, config=config),
            storage_dir=tmp_path, offline_dir=tmp_path / "offline",
        )

        r = scan_columns(tmp_path / "offline" / ".cache")["CleanMSTCNBiLSTMSegmenter"]
        assert len(r.steps) == 2
        # syringe 的存在性列：step 7 恒定、step 8 有变化 → 数据相关，不该判结构性
        assert "syringe_present" in r.data_dependent
        assert "syringe_present" not in r.structural_suspects
        # **恒定 ≠ 恒零**：本 fixture 里 hand 每帧同一个 bbox，present 恒等于 1 —— 永远是 1
        # 的列与永远是 0 的列一样不携带信息，都该被判出来。
        assert "hand_top1_present" in r.structural_suspects

    def test_contract_check_catches_undetectable_classes(self, tmp_path, monkeypatch):
        """先验契约检查：契约声明但检测器不产出的类别 → 必然恒零，应在配置期就抓住。

        这正是历史上 short_brush/long_brush/brush_tip_out 造成 45 列恒零的根因，
        统计诊断要跑完导出才发现，契约检查在加载 checkpoint 时就能报。
        """
        import app.services.inference.offline.diagnose as diag

        class _FakeModel:
            names = {0: "hand", 1: "syringe"}

        class _FakeYOLO:
            def __init__(self, path):
                self.model = _FakeModel()

        monkeypatch.setitem(
            __import__("sys").modules, "ultralytics", type("m", (), {"YOLO": _FakeYOLO})
        )
        ckpt = tmp_path / "fake.pt"
        ckpt.write_bytes(b"x")

        never_produced, never_consumed = diag.check_object_contract(
            ["hand", "syringe", "short_brush", "long_brush"], [ckpt]
        )
        assert never_produced == ["long_brush", "short_brush"]
        assert never_consumed == []

    def test_contract_check_reports_unconsumed_classes(self, tmp_path, monkeypatch):
        """反向：检测器产出但契约未消费 → 白丢的检测信号。"""
        import app.services.inference.offline.diagnose as diag

        class _FakeModel:
            names = {0: "hand", 1: "air_gun"}

        class _FakeYOLO:
            def __init__(self, path):
                self.model = _FakeModel()

        monkeypatch.setitem(
            __import__("sys").modules, "ultralytics", type("m", (), {"YOLO": _FakeYOLO})
        )
        ckpt = tmp_path / "fake.pt"
        ckpt.write_bytes(b"x")
        never_produced, never_consumed = diag.check_object_contract(["hand"], [ckpt])
        assert never_produced == [] and never_consumed == ["air_gun"]


# ============================ CLI ============================

class TestExportCli:
    @pytest.mark.parametrize("argv", [
        [],                                                    # 缺子命令
        ["nosuchcmd"],                                         # 未知子命令
        ["export", "--task-id", "1"],                          # 缺 --step-id
        ["export", "--step-id", "2"],                          # 缺 --task-id
        ["export", "--task-id", "1", "--step-id", "2", "--device", "tpu"],
        ["infer", "--task-id", "1", "--step-id", "2", "--device", "tpu"],
        ["contract"],                                          # 缺 --checkpoints
    ])
    def test_required_args_and_choices(self, argv):
        from app.services.inference.offline import cli

        with pytest.raises(SystemExit):
            cli.main(argv)

    def test_export_end_to_end_exit_zero(self, tmp_path, monkeypatch, capsys):
        """CLI 全链路：completed → 退出码 0，产物落在显式 --out-dir。"""
        from app.services.inference.config import InferenceConfig
        from app.services.inference.offline import cli
        from app.settings import settings

        _write_features(tmp_path, 7, 2, [1.0, 1.2, 1.4])
        monkeypatch.setattr(type(settings), "storage_base_dir", property(lambda _: tmp_path))
        monkeypatch.setattr(
            type(settings), "offline_base_dir", property(lambda _: tmp_path / "offline")
        )
        monkeypatch.setattr(
            "app.services.inference.config.load_stage_config",
            lambda *a, **k: InferenceConfig({"stages": {"2": {
                "detectors": [{"name": "clean_large"}],
                "offline": {"name": "s", "subscribes": ["clean_large"],
                            "class": _CLEAN_CLASS, "params": {}},
            }}}),
        )
        out = tmp_path / "cli_out"
        code = cli.main([
            "export", "--task-id", "7", "--step-id", "2", "--out-dir", str(out),
        ])
        assert code == 0
        assert "completed" in capsys.readouterr().out
        tag = "CleanMSTCNBiLSTMSegmenter"
        assert (out / f"input_{tag}.npz").exists()
        assert (out / f"manifest_{tag}.json").exists()
        # manifest 绝不叫 metadata.json：StorageCleanupWorker 按它判定 step 目录并 rmtree
        assert not (out / "metadata.json").exists()

    def test_export_error_exit_nonzero(self, tmp_path, monkeypatch):
        """策略加载失败 → 非 0 退出码（配置错不能伪装成成功）。"""
        from app.services.inference.config import InferenceConfig
        from app.services.inference.offline import cli
        from app.settings import settings

        _write_features(tmp_path, 7, 2, [1.0])
        monkeypatch.setattr(type(settings), "storage_base_dir", property(lambda _: tmp_path))
        monkeypatch.setattr(
            type(settings), "offline_base_dir", property(lambda _: tmp_path / "offline")
        )
        monkeypatch.setattr(
            "app.services.inference.config.load_stage_config",
            lambda *a, **k: InferenceConfig({"stages": {"2": {
                "detectors": [{"name": "clean_large"}],
                "offline": {"name": "s", "subscribes": ["clean_large"],
                            "class": "bogus_no_dots", "params": {}},
            }}}),
        )
        assert cli.main(["export", "--task-id", "7", "--step-id", "2"]) == 1

    def test_cpu_isolation_disables_gpu(self, monkeypatch):
        """device=cpu 时置空 CUDA_VISIBLE_DEVICES（不抢在线资源）；cuda 时不置。"""
        from app.services.inference.offline.cli import _isolate

        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
        _isolate("cpu", 2)
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == ""

        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
        _isolate("cuda", 2)
        assert os.environ.get("CUDA_VISIBLE_DEVICES") is None

    def test_manifest_json_is_valid(self, tmp_path):
        """manifest 是可解析 JSON 且带足溯源（哪套特征、哪个 backbone、多少维）。"""
        import argparse
        from app.services.inference.config import InferenceConfig
        from app.services.inference.offline import runner

        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        config = InferenceConfig({"stages": {"2": {
            "detectors": [{"name": "clean_large"}],
            "offline": {"name": "s", "subscribes": ["clean_large"],
                        "class": _CLEAN_CLASS, "params": {}},
        }}})
        runner.run_export(
            argparse.Namespace(task_id=7, step_id=2, segmenter=None, device="cpu",
                               threads=1, cache_ttl_days=30, out_dir=None, config=config),
            storage_dir=tmp_path, offline_dir=tmp_path / "offline",
        )
        path = (tmp_path / "offline" / ".cache" / "7" / "2"
                / "manifest_CleanMSTCNBiLSTMSegmenter.json")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["segmenter"].endswith("CleanMSTCNBiLSTMSegmenter")
        assert manifest["backbone"] == "none"
        assert manifest["feature_dim"] == 71
        assert manifest["quality"]["frames_total"] == 2
        assert "decode_short" in manifest["quality"]  # 早先的 ExportQuality 抄漏了这项
