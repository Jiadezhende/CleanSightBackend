"""离线分割入口测试：分段替换 / 配置工厂 / Runner / clean 策略 / 离线可跑校验 / CLI。

不依赖 GPU / RTSP / DB / 网络；storage 与 config 全用临时件，用例间不串。
"""

import json
import os
from pathlib import Path

import pytest

from doubles import BrushRulesSegmenter
from factories import make_det_box, make_detector_output, make_frame_detection, make_run

from app.types.detection import DetectorOutput
from app.types.temporal import TemporalEvent, TemporalSegment
from app.services.inference.config import InferenceConfig
from app.services.inference.offline.segmentation.segmenter import OfflineSegmenter
from app.services.inference.offline.segmentation.runner import OfflineRunner, OfflineRunSpec
from app.services.inference.stage_factory import StageFactory
from app.storage import inference as inference_store
from app.types.exceptions import ValidationError

_RULES_CLASS = "doubles.BrushRulesSegmenter"  # 测试替身，见 tests/doubles.py


def _frames(per_source):
    """{src: [DetectorOutput 按 ts]} → List[FrameDetection]（按 ts 对齐+升序），供直调 preprocess。"""
    by_ts: dict = {}
    for src, fds in per_source.items():
        for fd in fds:
            by_ts.setdefault(fd.timestamp, {})[src] = fd
    return [make_frame_detection(ts=ts, by_source=by_ts[ts]) for ts in sorted(by_ts)]


def _seg(producer="p", label="x", start=0.0, end=1.0):
    return TemporalSegment(producer=producer, label=label, start=start, end=end)


class TestReplaceSegments:
    """Runner 的 read → 合并 → write：数据层只管整体替换，保留谁是这里的事。"""

    def test_empty_clears_segments(self, tmp_storage):
        OfflineRunner._replace_segments(make_run(1, 1), [_seg()])
        OfflineRunner._replace_segments(make_run(1, 1), [])  # 空 → 清该 step 分段
        segs = [f for f in inference_store.read_temporal(make_run(1, 1)) if isinstance(f, TemporalSegment)]
        assert segs == []


# ============================ 配置 + 工厂 ============================

def _config(offline):
    return InferenceConfig({"stages": {"2": {
        "detectors": [{"name": "clean_large"}, {"name": "clean_small"}],
        "offline": offline,
    }}})


_OFFLINE_OK = {
    "class": _RULES_CLASS,
    "params": {"label": "brushing"},
}
# 测试替身策略（定义在本文件下方）：Boom 抛异常，Marker 验证 preprocess 预留层
_BOOM = {"class": "test_offline_pipeline.BoomSegmenter"}
_MARKER = {"class": "test_offline_pipeline.MarkerSegmenter"}
_CONTEXT = {"class": "test_offline_pipeline.ContextSegmenter"}


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
        "doubles.NoSuchSegmenter",
    ])
    def test_unimportable_class_fail_fast(self, class_path):
        offline = dict(_OFFLINE_OK, **{"class": class_path})
        with pytest.raises((ImportError, AttributeError)):
            StageFactory(_config(offline)).create_offline_segmenter("2")

    def test_enabled_builds_segmenter(self):
        seg = StageFactory(_config(_OFFLINE_OK)).create_offline_segmenter("2")
        assert isinstance(seg, BrushRulesSegmenter)
        assert seg.name == "BrushRulesSegmenter"  # producer = 类名
        assert seg.label == "brushing"


# ============================ 离线可跑校验 ============================

class TestRequireOffline:
    """离线无兜底：未定义 / offline 为空的 step 都是参数错误。"""

    def test_configured_returns_stage_key(self):
        assert _config(_OFFLINE_OK).require_offline(2) == "2"

    @pytest.mark.parametrize("cfg,step_id,reason", [
        (_config(_OFFLINE_OK), 999, "未在推理配置中定义"),
        (_config(_OFFLINE_OK), -1, "未在推理配置中定义"),   # -1 不再有特殊含义
        (_config({}), 2, "未配置离线模型"),
    ])
    def test_unrunnable_rejected(self, cfg, step_id, reason):
        with pytest.raises(ValidationError, match=reason):
            cfg.require_offline(step_id)


# ============================ Runner ============================

def _runner(offline):
    return OfflineRunner(config=_config(offline))


def _inference_dir(task_id, step_id):
    from app.storage.utils import root as _root

    return _root.run_path(make_run(task_id, step_id), "inference")


def _facts_path(root, task_id=1, step_id=2):
    return _inference_dir(task_id, step_id) / "temporal.jsonl"


def _probs_path(root, task_id=1, step_id=2):
    return _inference_dir(task_id, step_id) / "label_probs.npz"


def _write_detections(task_id, step_id):
    """经数据层预置两帧双源检测结果（storage 根已由 tmp_storage fixture 指到临时目录）。"""
    inference_store.append_detections(make_run(task_id, step_id), [
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
        segs = [f for f in inference_store.read_temporal(make_run(1, 2)) if isinstance(f, TemporalSegment)]
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
        segs = [f for f in inference_store.read_temporal(make_run(1, 2)) if isinstance(f, TemporalSegment)]
        assert len(segs) == 1

    def test_strategy_exception_propagates_no_write(self, tmp_storage):
        _write_detections(1, 2)
        with pytest.raises(RuntimeError):
            _runner(_BOOM).run(OfflineRunSpec(task_id=1, step_id=2))
        assert not _facts_path(tmp_storage).exists()

    def test_model_swap_replaces_old_segments_keeps_eventfact(self, tmp_storage):
        """换模型重跑：旧类名的分段整体被替换，TemporalEvent 保留。"""
        _write_detections(1, 2)
        inference_store.write_temporal(make_run(1, 2), [TemporalEvent(producer="clean_monitor", signal="sig", value=1, ts=1.0)])
        assert _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2)).producer == "BrushRulesSegmenter"
        res = _runner(_MARKER).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.producer == "MarkerSegmenter"
        loaded = inference_store.read_temporal(make_run(1, 2))
        assert {f.producer for f in loaded if isinstance(f, TemporalSegment)} == {"MarkerSegmenter"}
        assert [f.producer for f in loaded if isinstance(f, TemporalEvent)] == ["clean_monitor"]

    def test_label_probs_bypass_written_with_facts(self, tmp_storage):
        """策略产逐帧概率：temporal.jsonl 与 label_probs.npz 都落盘，概率原样（float16 精度内）。"""
        _write_detections(1, 2)
        res = _runner({"class": f"{__name__}.ProbsSegmenter"}).run(OfflineRunSpec(task_id=1, step_id=2))
        assert (res.status, res.segment_count) == ("completed", 1)
        probs = inference_store.read_label_probs(make_run(1, 2))
        assert probs.labels == ("idle", "x") and probs.ts.tolist() == [1.0, 2.0]
        assert probs.probs[:, 1].tolist() == [0.75, 0.75]

    def test_context_strategy_gets_video_bound_to_locked_run(self, tmp_storage, monkeypatch):
        """`uses_context` 策略经 context.video 取帧：参数原样透传给 hls.iter_frames，run 是锁定的那个。"""
        from app.services.inference.offline.segmentation import runner as runner_mod

        calls = []
        monkeypatch.setattr(runner_mod.hls, "iter_frames", lambda run, **kw: calls.append((run, kw)) or iter(()))
        _write_detections(1, 2)
        res = _runner(_CONTEXT).run(OfflineRunSpec(task_id=1, step_id=2))
        assert res.status == "completed"
        assert calls == [(make_run(1, 2), {"width": 640, "height": 480})]

    def test_context_cache_roundtrips_and_degrades_to_miss(self, tmp_storage):
        """缓存经 context 存取；读坏了当未命中，写失败（run 已回收）只告警不抛。"""
        import numpy as np
        from app.types.run import RunIdentity

        _write_detections(1, 2)
        run = make_run(1, 2)
        ctx = OfflineRunner._context(run)
        assert ctx.load_cache("c") is None
        ctx.save_cache("c", {"a": np.arange(3)})
        assert ctx.load_cache("c")["a"].tolist() == [0, 1, 2]
        (_inference_dir(1, 2) / "cache_c.npz").write_bytes(b"broken")
        assert ctx.load_cache("c") is None
        OfflineRunner._context(RunIdentity(1, 2, run.run_id + 1)).save_cache("c", {"a": np.zeros(1)})

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


class ContextSegmenter(OfflineSegmenter):
    """`uses_context`：preprocess 收第二个参数，并真的经它取一次帧。"""

    uses_context = True

    def preprocess(self, frames, context):
        return list(context.video(width=640, height=480))

    def segment(self, model_input):
        return [TemporalSegment(producer=self.name, label="c", start=0.0, end=1.0)]


# ============================ CLI ============================

class TestRunLock:
    def test_named_missing_run_is_reclaimed_and_writes_nothing(self, tmp_storage):
        _write_detections(1, 2)
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2, run_id=123))
        assert (res.status, res.segment_count) == ("reclaimed", 0)
        assert not _facts_path(tmp_storage).exists()

    def test_run_reclaimed_mid_run_is_reclaimed_and_not_rebuilt(self, tmp_storage, monkeypatch):
        """跑到一半所在 step 过 TTL 被回收：写入 FileNotFoundError → reclaimed，不重建目录。"""
        import shutil

        from app.services.inference.offline.segmentation import runner as runner_mod
        from app.storage.utils import root as _root

        _write_detections(1, 2)
        run = make_run(1, 2)
        real_validate = runner_mod.OfflineRunner._validate

        def reclaim_then_validate(facts, producer):
            shutil.rmtree(_root.path(1, 2))
            return real_validate(facts, producer)

        monkeypatch.setattr(runner_mod.OfflineRunner, "_validate", staticmethod(reclaim_then_validate))
        res = _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2, run_id=run.run_id))
        assert res.status == "reclaimed"
        assert not _root.path(1, 2).exists()

    def test_named_older_run_is_the_one_written(self, tmp_storage):
        """锁定点名的 run：同 step 已有更新的 run，结果仍写回旧 run。"""
        from app.types.run import RunIdentity
        from app.storage.utils import root as _root

        _write_detections(1, 2)
        old = make_run(1, 2)
        new = RunIdentity(1, 2, old.run_id + 1)
        _root.run_path(new).mkdir()
        inference_store.append_detections(new, [make_frame_detection(ts=9.0)])

        _runner(_OFFLINE_OK).run(OfflineRunSpec(task_id=1, step_id=2, run_id=old.run_id))

        assert [f.producer for f in inference_store.read_temporal(old)] == ["BrushRulesSegmenter"]
        assert inference_store.read_temporal(new) == []


class TestCli:
    @pytest.fixture(autouse=True)
    def _no_isolation(self, monkeypatch):
        # _isolate 会在 pytest 主进程里永久改 CUDA_VISIBLE_DEVICES 与 torch 线程数，污染后续用例
        from app.services.inference.offline import cli
        monkeypatch.setattr(cli, "_isolate", lambda num_threads: None)

    def test_pin_device_uses_online_inference_card(self, monkeypatch):
        """离线子进程与在线推理子进程钉同一张卡（settings.cuda_device），不碰机器上别的卡。"""
        from app.services.inference.offline import cli
        from app.settings import settings

        monkeypatch.setattr(settings, "cuda_device", "1")
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1,2")
        cli._pin_device()
        assert os.environ["CUDA_VISIBLE_DEVICES"] == "1"

    def test_run_completed_json_last_line(self, tmp_storage, monkeypatch, capsys):
        """stdout 末行恒为结果 JSON（作业服务按此解析）。

        默认路径：OfflineRunner() 用 settings.storage_base_dir（tmp_storage 已指临时目录）
        + runner 内 load_stage_config（monkeypatch 成临时 config，绕开单例）。
        """
        from app.services.inference import config as config_mod
        from app.services.inference.offline.segmentation import runner as runner_mod
        _write_detections(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        monkeypatch.setattr(config_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2"])
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 0
        assert json.loads(last) == {
            "status": "completed", "producer": "BrushRulesSegmenter", "segment_count": 1, "alarm_count": None,
            "message": "",
        }

    def test_run_check_failure_keeps_completed(self, tmp_storage, monkeypatch, capsys):
        """规则核对失败（这里是类表不符）只进 message：分割结果照常 completed、退出码 0、alarm_count 为 null。"""
        from app.services.inference import config as config_mod
        from app.services.inference.offline.segmentation import runner as runner_mod
        _write_detections(1, 2)
        cfg = InferenceConfig({"stages": {"2": {
            "offline": {"class": f"{__name__}.ProbsSegmenter"},
            "compliance": {
                "evidence": {"episodes": {"strict": {"p_on": 0.6, "p_off": 0.4, "merge_gap_s": 1, "min_len_s": 1},
                                          "lenient": {"p_on": 0.4, "p_off": 0.2, "merge_gap_s": 1, "min_len_s": 1}}},
                "groups": {"flush": ["flush"]},
                "rules": [{"id": "m", "type": "presence", "group": "flush", "present_s": 1, "absent_s": 0,
                           "message": "x"}],
            },
        }}})
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: cfg)
        monkeypatch.setattr(config_mod, "load_stage_config", lambda *a, **k: cfg)
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 0
        assert (payload["status"], payload["alarm_count"]) == ("completed", None)
        assert payload["message"].startswith("规则核对失败") and not (_inference_dir(1, 2) / "alarms.jsonl").exists()

    def test_run_strategy_error_exit_nonzero(self, tmp_storage, monkeypatch, capsys):
        from app.services.inference.offline.segmentation import runner as runner_mod
        _write_detections(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_BOOM))
        from app.services.inference.offline import cli
        rc = cli.main(["run", "--task-id", "1", "--step-id", "2"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 1
        assert payload["status"] == "error" and payload["message"] == "boom"

    def test_query_roundtrip(self, tmp_storage, monkeypatch, capsys):
        """run 写出 facts 后，query 子命令能读回时间线。"""
        from app.services.inference.offline.segmentation import runner as runner_mod
        _write_detections(1, 2)
        monkeypatch.setattr(runner_mod, "load_stage_config", lambda *a, **k: _config(_OFFLINE_OK))
        from app.services.inference.offline import cli
        assert cli.main(["run", "--task-id", "1", "--step-id", "2"]) == 0
        capsys.readouterr()  # 清 run 的输出
        rc = cli.main(["query", "--task-id", "1", "--step-id", "2"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert (payload["task_id"], payload["run_id"]) == (1, make_run(1, 2).run_id)
        assert [row["label"] for row in payload["timeline"]] == ["brushing"]


class ProbsSegmenter(OfflineSegmenter):
    """产逐帧概率的替身：一段 + 与输入帧对齐的 label_probs。"""

    def preprocess(self, frames):
        return [f.ts for f in frames]

    def segment(self, ts):
        import numpy as np
        from app.types.temporal import LabelProbs
        self._probs = LabelProbs(ts=np.asarray(ts), probs=np.tile([0.25, 0.75], (len(ts), 1)), labels=("idle", "x"))
        return [TemporalSegment(producer=self.name, label="x", start=ts[0], end=ts[-1], conf=0.75)]

    def label_probs(self):
        return self._probs


class MaterialSegmenter(OfflineSegmenter):
    """声明物料的替身：`files` 原样作为 materials()。"""

    def __init__(self, files=()):
        self.files = [Path(f) for f in files]

    def materials(self):
        return self.files

    def preprocess(self, frames):
        return frames

    def segment(self, model_input):
        return []


def _materials_config(tmp_path, *names):
    (tmp_path / "have.pt").write_bytes(b"x")
    return _config({"class": f"{__name__}.MaterialSegmenter", "params": {"files": [str(tmp_path / n) for n in names]}})


class TestMaterials:
    def test_lists_active_strategy_materials_per_stage(self, tmp_path):
        from app.services.inference.offline.segmentation.materials import check_materials

        statuses = check_materials(_materials_config(tmp_path, "have.pt", "gone.pt"))
        assert [(s.stage, s.producer, s.path.name, s.exists) for s in statuses] == [
            ("2", "MaterialSegmenter", "have.pt", True), ("2", "MaterialSegmenter", "gone.pt", False),
        ]

    def test_stage_without_offline_and_default_strategy_list_nothing(self):
        from app.services.inference.offline.segmentation.materials import check_materials

        assert check_materials(_config({})) == []
        assert check_materials(_config(_OFFLINE_OK)) == []

    @pytest.mark.parametrize("names,rc", [(("have.pt",), 0), (("have.pt", "gone.pt"), 1)])
    def test_cli_exit_code_reflects_missing(self, tmp_path, monkeypatch, capsys, names, rc):
        from app.services.inference import config as config_mod
        from app.services.inference.offline import cli

        monkeypatch.setattr(config_mod, "load_stage_config", lambda *a, **k: _materials_config(tmp_path, *names))
        assert cli.main(["materials"]) == rc
        assert [row["exists"] for row in json.loads(capsys.readouterr().out)] == [n == "have.pt" for n in names]

    def test_service_start_warns_missing_but_starts(self, tmp_path, caplog):
        from app.services.inference.offline.service import OfflineJobService

        svc = OfflineJobService(config=_materials_config(tmp_path, "have.pt", "gone.pt"), clients=object())
        svc.start()
        try:
            assert "离线物料缺失" in caplog.text and "gone.pt" in caplog.text and "have.pt" not in caplog.text
        finally:
            svc.stop()


class BadProbsSegmenter(OfflineSegmenter):
    """label_probs 行数与 ts 对不上的策略：验证 Runner 旁路防护。"""

    def preprocess(self, frames):
        return frames

    def segment(self, model_input):
        return []

    def label_probs(self):
        import numpy as np
        from app.types.temporal import LabelProbs
        return LabelProbs(ts=np.array([1.0, 2.0]), probs=np.zeros((3, 2)), labels=("a", "b"))
