"""离线特征导出链路测试：raw 帧索引 sidecar / R0 recipe / ExportRunner / CLI 参数。

不依赖 GPU / ffmpeg / RTSP / DB：sidecar 的构造与解析是纯函数（`cv2.VideoWriter` 写段、
段解码属 I/O 边界，按开发规范留给集成测试）；导出链路全用临时 storage。
"""

import json
import os

import numpy as np
import pytest

from factories import make_frame_detections, make_frame_feature

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
        assert got.feature_version == want.feature_version == "clean_bbox_v2_top1_impute"
        np.testing.assert_array_equal(
            np.asarray(got.features, dtype=np.float32),
            np.asarray(want.features, dtype=np.float32),
        )

    def test_ignores_visual_argument(self):
        """R0 不消费像素；统一签名只为让导出器一视同仁地调用。"""
        frames = [make_frame_feature(ts=1.0, source="clean_large")]
        assert export_r0(frames, None).features == export_r0(frames, object()).features

    def test_base_dim_is_113(self):
        frames = [make_frame_feature(ts=float(i), source="clean_large") for i in range(3)]
        out = export_r0(frames)
        assert out.feature_dim == 113 == len(out.feature_names)


# ============================ ExportRunner ============================

class TestExportRunner:
    def test_completed_writes_npz_and_manifest(self, tmp_path):
        ts_list = [1.0, 1.2, 1.4, 1.6]
        _write_features(tmp_path, 7, 2, ts_list)

        result = ExportRunner(tmp_path).run(ExportSpec(task_id=7, step_id=2, recipe=_R0))

        assert result.status == "completed"
        assert result.frame_count == len(ts_list)
        assert result.feature_dim == 113

        npz = np.load(result.out_dir / "input.npz")
        assert npz["features"].shape == (len(ts_list), 113)
        assert npz["timestamps"].tolist() == ts_list

        manifest = json.loads((result.out_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["recipe"] == _R0
        assert manifest["backbone"] == "none"
        assert manifest["feature_version"] == "clean_bbox_v2_top1_impute"
        assert manifest["feature_dim"] == len(manifest["feature_names"]) == 113
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

    def test_backbone_not_yet_supported(self, tmp_path):
        """视觉分支（帧源 + backbone）留到 R1a/R1b；此前传 --backbone 须显式报错而非静默降级。"""
        _write_features(tmp_path, 7, 2, [1.0, 1.2])
        with pytest.raises(NotImplementedError):
            ExportRunner(tmp_path).run(
                ExportSpec(task_id=7, step_id=2, recipe=_R0, backbone="yolo")
            )


class TestRecipeShortName:
    @pytest.mark.parametrize("path,want", [
        ("app.services.inference.offline.impl.clean.export_r0", "r0"),
        ("pkg.mod.export_r1a", "r1a"),
        ("pkg.mod.custom_fn", "custom_fn"),
    ])
    def test_short_name(self, path, want):
        assert recipe_short_name(path) == want


# ============================ CLI ============================

class TestExportCli:
    @pytest.mark.parametrize("argv", [
        ["--task-id", "1"],                                   # 缺 --step-id / --recipe
        ["--step-id", "2", "--recipe", _R0],                  # 缺 --task-id
        ["--task-id", "1", "--step-id", "2"],                 # 缺 --recipe
        ["--task-id", "1", "--step-id", "2", "--recipe", _R0, "--device", "tpu"],  # 非法设备
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
            "--task-id", "7", "--step-id", "2", "--recipe", _R0, "--out-dir", str(out),
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
            "--task-id", "7", "--step-id", "2", "--recipe", "bogus_no_dots",
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
