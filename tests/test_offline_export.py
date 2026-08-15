"""离线特征导出链路测试：raw 帧索引 sidecar / R0 recipe / ExportRunner / CLI 参数。

不依赖 GPU / ffmpeg / RTSP / DB：sidecar 的构造与解析是纯函数（`cv2.VideoWriter` 写段、
段解码属 I/O 边界，按开发规范留给集成测试）；导出链路全用临时 storage。
"""

import json
import os

import numpy as np
import pytest

from factories import make_detection, make_frame_detections, make_frame_feature

from app.domain.detection import FrameDetections
from app.domain.frame import Frame
from app.services.inference.feature.store import FeatureStore
from app.services.inference.offline.export.models import ExportSpec
from app.services.inference.offline.export.runner import (
    ExportRunner,
    recipe_short_name,
)
from app.services.inference.offline.impl.clean import build_base_features, export_r0
from app.services.persistence.strategies.raw_frame_index import (
    build_frame_index,
    index_path_for,
    read_frame_index,
    write_frame_index,
)

_R0 = "app.services.inference.offline.impl.clean.export_r0"
_R1 = "app.services.inference.offline.impl.clean.export_r1"


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


# ============================ R0 recipe ============================

class TestR0Recipe:
    def test_identical_to_build_base_features(self):
        """**单一真源自证**：导出走的 R0 与直接算的基础特征逐值相等。

        导出器与将来的融合 Segmenter.preprocess 调同一批函数，训练样例与线上特征转换
        因此不可能漂移。
        """
        frames = [
            make_frame_feature(
                ts=float(i),
                by_source={"clean_large": make_frame_detections(n=2, class_name="hand", ts=float(i))},
                frame_width=640, frame_height=480,
            )
            for i in range(1, 6)
        ]
        got = export_r0(frames)
        want = build_base_features(frames, 7.5, 640, 480)
        assert got.feature_names == want.feature_names
        assert got.feature_version == want.feature_version == "clean_bbox_v3_detectable"
        np.testing.assert_array_equal(
            np.asarray(got.features, dtype=np.float32),
            np.asarray(want.features, dtype=np.float32),
        )

    def test_ignores_visual_argument(self):
        """R0 不消费像素；统一签名只为让导出器一视同仁地调用。"""
        frames = [make_frame_feature(ts=1.0, source="clean_large")]
        assert export_r0(frames, None).features == export_r0(frames, object()).features

    def test_base_dim_is_71(self):
        frames = [make_frame_feature(ts=float(i), source="clean_large") for i in range(3)]
        out = export_r0(frames)
        assert out.feature_dim == 71 == len(out.feature_names)


# ============================ ExportRunner ============================

class TestExportRunner:
    def test_completed_writes_npz_and_manifest(self, tmp_path):
        ts_list = [1.0, 1.2, 1.4, 1.6]
        _write_features(tmp_path, 7, 2, ts_list)

        result = ExportRunner(tmp_path).run(ExportSpec(task_id=7, step_id=2, recipe=_R0))

        assert result.status == "completed"
        assert result.frame_count == len(ts_list)
        assert result.feature_dim == 71

        npz = np.load(result.out_dir / "input.npz")
        assert npz["features"].shape == (len(ts_list), 71)
        assert npz["timestamps"].tolist() == ts_list

        manifest = json.loads((result.out_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["recipe"] == _R0
        assert manifest["backbone"] == "none"
        assert manifest["feature_version"] == "clean_bbox_v3_detectable"
        assert manifest["feature_dim"] == len(manifest["feature_names"]) == 71
        assert manifest["frame_count"] == len(ts_list)
        assert manifest["ts_start"] == ts_list[0] and manifest["ts_end"] == ts_list[-1]
        assert manifest["quality"]["frames_total"] == len(ts_list)
        assert manifest["quality"]["needs_pixels"] is False

    def test_manifest_is_not_named_metadata_json(self, tmp_path):
        """产物目录内不得出现 metadata.json。

        StorageCleanupWorker 按 `{base}/*/*/metadata.json` 判定过期 step 目录并 rmtree
        整个目录 —— 重名会让导出产物在 TTL 到期时被静默删掉。
        """
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        result = ExportRunner(tmp_path).run(ExportSpec(task_id=7, step_id=2, recipe=_R0))
        assert not (result.out_dir / "metadata.json").exists()
        assert (result.out_dir / "manifest.json").exists()

    def test_out_dir_outside_ttl_swept_step_dir(self, tmp_path):
        """产物不落在 `{base}/{task}/{step}/` —— 那里受 cleanup_days TTL 回收。"""
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        result = ExportRunner(tmp_path).run(ExportSpec(task_id=7, step_id=2, recipe=_R0))
        step_dir = tmp_path / "7" / "2"
        assert step_dir not in result.out_dir.parents and result.out_dir != step_dir
        assert result.out_dir == tmp_path / ".offline_exports" / "7" / "2" / "r0@none"

    def test_explicit_out_dir_overrides(self, tmp_path):
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        target = tmp_path / "elsewhere"
        result = ExportRunner(tmp_path).run(
            ExportSpec(task_id=7, step_id=2, recipe=_R0, out_dir=target)
        )
        assert result.out_dir == target and (target / "input.npz").exists()

    def test_no_features_skipped_without_writing(self, tmp_path):
        result = ExportRunner(tmp_path).run(ExportSpec(task_id=99, step_id=2, recipe=_R0))
        assert result.status == "skipped"
        assert result.out_dir is None
        assert not (tmp_path / ".offline_exports").exists()

    def test_rerun_overwrites(self, tmp_path):
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        runner = ExportRunner(tmp_path)
        spec = ExportSpec(task_id=7, step_id=2, recipe=_R0)
        first = runner.run(spec)
        second = runner.run(spec)
        assert first.out_dir == second.out_dir
        assert second.status == "completed"

    @pytest.mark.parametrize("bad", [
        "no_dots_here",
        "app.services.inference.offline.impl.clean.does_not_exist",
        "app.nonexistent.module.fn",
        "app.services.inference.offline.impl.clean.FEATURE_VERSION",  # 不可调用
    ])
    def test_bad_recipe_fails_fast(self, tmp_path, bad):
        """recipe 加载失败一律 fail-fast —— 导出跑空比报错更难查。"""
        _write_features(tmp_path, 7, 2, [1.0])
        with pytest.raises(ValueError):
            ExportRunner(tmp_path).run(ExportSpec(task_id=7, step_id=2, recipe=bad))

    def test_no_pixels_available_fails_loudly(self, tmp_path):
        """没有任何 raw 段/sidecar 时，视觉分支硬失败并报出分项原因。

        绝不产出"全零视觉特征"的样例——那会让训练侧以为拿到了视觉信息。
        （sidecar 落地之前录的 step 正是这种情况。）
        """
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        with pytest.raises(RuntimeError, match="没有取到任何像素帧"):
            ExportRunner(tmp_path).run(
                ExportSpec(task_id=7, step_id=2, recipe=_R1, backbone="yolo")
            )

    def test_r1_without_backbone_fails(self, tmp_path):
        """R1 缺 visual 时硬失败，不静默退化成 R0（否则 R1 vs R0 的对照会变成自己比自己）。"""
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        with pytest.raises(ValueError, match="global_vec"):
            ExportRunner(tmp_path).run(ExportSpec(task_id=7, step_id=2, recipe=_R1))


class TestRecipeShortName:
    @pytest.mark.parametrize("path,want", [
        ("app.services.inference.offline.impl.clean.export_r0", "r0"),
        ("pkg.mod.export_r1a", "r1a"),
        ("pkg.mod.custom_fn", "custom_fn"),
    ])
    def test_short_name(self, path, want):
        assert recipe_short_name(path) == want


# ============================ 特征健康诊断 ============================

class TestDiagnose:
    """无效特征检测：结构性无效（到处恒定）vs 数据相关无效（这条片段恰好没出现）。"""

    def _export(self, tmp_path, task_id, ts_list):
        _write_features(tmp_path, task_id, 2, ts_list)
        return ExportRunner(tmp_path).run(
            ExportSpec(task_id=task_id, step_id=2, recipe=_R0)
        )

    def test_single_step_cannot_judge_structural(self, tmp_path):
        """**只有 1 个 step 时无法区分两类无效** —— 报告须显式标注这个局限。

        单条片段上的恒定列，既可能是契约声明了检不出的类别（结构性），也可能只是
        这段视频没出现该目标（数据相关）。不加警示就会把后者误删。
        """
        from app.services.inference.offline.export.diagnose import format_report, scan_columns

        self._export(tmp_path, 7, [1.0, 1.2, 1.4])
        reports = scan_columns(tmp_path / ".offline_exports")
        assert list(reports) == ["r0@none"]
        assert len(reports["r0@none"].steps) == 1
        assert "只有 1 个 step" in format_report(reports)

    def test_cross_step_separates_structural_from_data_dependent(self, tmp_path):
        """跨 step 聚合才有判定力：全 step 恒定 = 结构性可疑，部分恒定 = 数据相关。"""
        from app.services.inference.offline.export.diagnose import scan_columns

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
        ExportRunner(tmp_path).run(ExportSpec(task_id=8, step_id=2, recipe=_R0))

        r = scan_columns(tmp_path / ".offline_exports")["r0@none"]
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
        import app.services.inference.offline.export.diagnose as diag

        class _FakeModel:
            names = {0: "hand", 1: "syringe"}

        class _FakeYOLO:
            def __init__(self, path):
                self.model = _FakeModel()

        fake_ultra = type("m", (), {"YOLO": _FakeYOLO})
        monkeypatch.setitem(__import__("sys").modules, "ultralytics", fake_ultra)
        ckpt = tmp_path / "fake.pt"
        ckpt.write_bytes(b"x")

        never_produced, never_consumed = diag.check_object_contract(
            ["hand", "syringe", "short_brush", "long_brush"], [ckpt]
        )
        assert never_produced == ["long_brush", "short_brush"]
        assert never_consumed == []

    def test_contract_check_reports_unconsumed_classes(self, tmp_path, monkeypatch):
        """反向：检测器产出但契约未消费 → 白丢的检测信号。"""
        import app.services.inference.offline.export.diagnose as diag

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
        [],                                                          # 缺子命令
        ["nosuchcmd"],                                               # 未知子命令
        ["run", "--task-id", "1"],                                   # 缺 --step-id / --recipe
        ["run", "--step-id", "2", "--recipe", _R0],                  # 缺 --task-id
        ["run", "--task-id", "1", "--step-id", "2"],                 # 缺 --recipe
        ["run", "--task-id", "1", "--step-id", "2", "--recipe", _R0, "--device", "tpu"],
        ["contract"],                                                # 缺 --checkpoints
    ])
    def test_required_args_and_choices(self, argv):
        from app.services.inference.offline.export import cli

        with pytest.raises(SystemExit):
            cli.main(argv)

    def test_run_end_to_end_exit_zero(self, tmp_path, monkeypatch, capsys):
        """CLI 全链路：completed → 退出码 0，产物落在显式 --out-dir。"""
        from app.settings import settings
        from app.services.inference.offline.export import cli

        _write_features(tmp_path, 7, 2, [1.0, 1.2, 1.4])
        monkeypatch.setattr(type(settings), "storage_base_dir", property(lambda _: tmp_path))
        out = tmp_path / "cli_out"

        code = cli.main([
            "run", "--task-id", "7", "--step-id", "2", "--recipe", _R0, "--out-dir", str(out),
        ])
        assert code == 0
        assert "completed" in capsys.readouterr().out
        assert (out / "input.npz").exists() and (out / "manifest.json").exists()

    def test_bad_recipe_exit_nonzero(self, tmp_path, monkeypatch):
        """recipe 异常 → 非 0 退出码（配置错不能伪装成成功）。"""
        from app.settings import settings
        from app.services.inference.offline.export import cli

        _write_features(tmp_path, 7, 2, [1.0])
        monkeypatch.setattr(type(settings), "storage_base_dir", property(lambda _: tmp_path))
        assert cli.main([
            "run", "--task-id", "7", "--step-id", "2", "--recipe", "bogus_no_dots",
        ]) == 1

    def test_cpu_isolation_disables_gpu(self, monkeypatch):
        """device=cpu 时置空 CUDA_VISIBLE_DEVICES（不抢在线资源）；cuda 时不置。"""
        from app.services.inference.offline.export.cli import _isolate

        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
        _isolate("cpu", 2)
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == ""

        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
        _isolate("cuda", 2)
        assert os.environ.get("CUDA_VISIBLE_DEVICES") is None
