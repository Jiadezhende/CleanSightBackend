"""run 目录：`runs.allocate` / `runs.query`，以及域读写口收 `RunIdentity` 的落位与建目录边界。"""

import dataclasses

import numpy as np
import pytest

from factories import make_frame
from app.domain.detection import DetectorOutput, FrameDetection
from app.domain.run import RunIdentity
from app.domain.temporal import LabelProbs, TemporalSegment
from app.storage import hls, inference, runs


def _fd(ts):
    return FrameDetection(ts=ts, by_source={"s": DetectorOutput(boxes=[], metadata={}, timestamp=ts)})


def _seg():
    return TemporalSegment(producer="p", label="a", start=0.0, end=1.0)


def _probs():
    return LabelProbs(ts=np.array([1.0]), probs=np.array([[1.0]]), labels=("a",))


# ---------------------------------------------------------------------------
# RunIdentity
# ---------------------------------------------------------------------------


class TestRunIdentity:
    def test_value_equality_and_hashable(self):
        assert RunIdentity(1, 2, 3) == RunIdentity(1, 2, 3)
        assert len({RunIdentity(1, 2, 3), RunIdentity(1, 2, 3)}) == 1
        assert RunIdentity(1, 2, 3) != RunIdentity(1, 2, 4)

    def test_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            RunIdentity(1, 2, 3).run_id = 4


# ---------------------------------------------------------------------------
# allocate
# ---------------------------------------------------------------------------


class TestAllocate:
    def test_creates_run_dir_under_step(self, tmp_storage):
        run = runs.allocate(1, 2)
        assert (run.task_id, run.step_id) == (1, 2)
        assert (tmp_storage / "1" / "2" / str(run.run_id)).is_dir()

    def test_run_id_is_microseconds_now(self, tmp_storage, monkeypatch):
        monkeypatch.setattr(runs.time, "time_ns", lambda: 1_700_000_000_123_456_789)
        assert runs.allocate(1, 2).run_id == 1_700_000_000_123_456

    def test_strictly_increasing_even_if_clock_stalls_or_goes_back(self, tmp_storage, monkeypatch):
        clock = iter([5_000_000, 5_000_000, 1_000_000])
        monkeypatch.setattr(runs.time, "time_ns", lambda: next(clock) * 1000)
        ids = [runs.allocate(1, 2).run_id for _ in range(3)]
        assert ids == [5_000_000, 5_000_001, 5_000_002]

    def test_legacy_domain_dirs_are_not_run_ids(self, tmp_storage, monkeypatch):
        (tmp_storage / "1" / "2" / "hls").mkdir(parents=True)
        monkeypatch.setattr(runs.time, "time_ns", lambda: 7_000)
        assert runs.allocate(1, 2).run_id == 7

    def test_steps_are_independent(self, tmp_storage, monkeypatch):
        monkeypatch.setattr(runs.time, "time_ns", lambda: 7_000)
        assert runs.allocate(1, 2).run_id == 7
        assert runs.allocate(1, 3).run_id == 7


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


def _alloc(monkeypatch, task, step, run_id):
    monkeypatch.setattr(runs.time, "time_ns", lambda: run_id * 1000)
    run = runs.allocate(task, step)
    assert run.run_id == run_id
    return run


class TestQuery:
    def test_named_existing_run(self, tmp_storage, monkeypatch):
        run = _alloc(monkeypatch, 1, 2, 10)
        # 点名要的就是它：空 run 也返回，不做可见判断
        assert runs.query(1, 2, 10) == run

    def test_named_missing_run_is_none(self, tmp_storage, monkeypatch):
        _alloc(monkeypatch, 1, 2, 10)
        assert runs.query(1, 2, 11) is None
        assert runs.query(9, 9, 10) is None

    def test_default_is_latest_visible(self, tmp_storage, monkeypatch):
        old = _alloc(monkeypatch, 1, 2, 10)
        inference.append_detections(old, [_fd(1.0)])
        mid = _alloc(monkeypatch, 1, 2, 20)
        inference.append_detections(mid, [_fd(2.0)])
        _alloc(monkeypatch, 1, 2, 30)   # 最新但还没产物：不可见

        assert runs.query(1, 2) == mid

    def test_hls_metadata_makes_a_run_visible(self, tmp_storage, monkeypatch):
        run = _alloc(monkeypatch, 1, 2, 10)
        hls_dir = hls.init_path(run, "raw").parent
        hls_dir.mkdir()
        (hls_dir / "raw_playlist.m3u8").write_text("#EXTM3U\n")
        assert runs.query(1, 2) is None           # 只有 playlist 不算
        (hls_dir / "metadata.json").write_text("{}")
        assert runs.query(1, 2) == run

    def test_no_visible_run_is_none(self, tmp_storage, monkeypatch):
        assert runs.query(1, 2) is None
        _alloc(monkeypatch, 1, 2, 10)
        assert runs.query(1, 2) is None

    def test_legacy_layout_is_invisible(self, tmp_storage):
        inference.append_detections(1, 2, [_fd(1.0)])   # 旧形态落在 {step}/inference/
        assert runs.query(1, 2) is None


# ---------------------------------------------------------------------------
# 域读写口收 RunIdentity：落位 + 写者不建 run 目录
# ---------------------------------------------------------------------------


class TestRunKeyedPorts:
    def test_inference_products_land_in_run_dir(self, tmp_storage, monkeypatch):
        run = _alloc(monkeypatch, 1, 2, 10)
        inference.append_detections(run, [_fd(1.0)])
        inference.write_temporal(run, [_seg()])
        inference.write_label_probs(run, _probs())

        d = tmp_storage / "1" / "2" / "10" / "inference"
        assert sorted(p.name for p in d.iterdir()) == ["detections.jsonl", "label_probs.npz", "temporal.jsonl"]
        assert [f.ts for f in inference.read_detections(run)] == [1.0]
        assert [f.label for f in inference.read_temporal(run)] == ["a"]
        assert inference.read_label_probs(run).labels == ("a",)
        # 旧形态读的是 {step}/inference/，看不到 run 里的产物
        assert inference.read_detections(1, 2) == []

    def test_runs_do_not_see_each_other(self, tmp_storage, monkeypatch):
        a = _alloc(monkeypatch, 1, 2, 10)
        b = _alloc(monkeypatch, 1, 2, 20)
        inference.append_detections(a, [_fd(1.0)])
        assert inference.read_detections(b) == []

    def test_hls_paths_land_in_run_dir(self, tmp_storage):
        run = RunIdentity(1, 2, 10)
        base = tmp_storage / "1" / "2" / "10" / "hls"
        ref = hls.SegmentRef("raw", 5)
        assert hls.segment_path(run, ref).parent == base
        assert hls.init_path(run, "raw").parent == base
        assert hls.playlist_path(run, "raw").parent == base
        assert hls.list_segments(run, "raw") == []

    @pytest.mark.parametrize("write", [
        lambda run: inference.append_detections(run, [_fd(1.0)]),
        lambda run: inference.write_temporal(run, [_seg()]),
        lambda run: inference.write_label_probs(run, _probs()),
        lambda run: hls.insert_segment(run, "raw", [make_frame(ts=1.0), make_frame(ts=1.1)]),
    ], ids=["append_detections", "write_temporal", "write_label_probs", "insert_segment"])
    def test_write_to_missing_run_dir_fails_and_creates_nothing(self, tmp_storage, write):
        """run 已被回收（或从没分配过）：写入在文件系统层失败，不重建出僵尸目录。"""
        with pytest.raises(FileNotFoundError):
            write(RunIdentity(1, 2, 10))
        assert list(tmp_storage.iterdir()) == []

    def test_write_after_run_dir_removed_fails(self, tmp_storage, monkeypatch):
        from app.storage import _fs

        run = _alloc(monkeypatch, 1, 2, 10)
        inference.append_detections(run, [_fd(1.0)])
        assert _fs.remove(tmp_storage / "1" / "2") is _fs.Removed.REMOVED

        with pytest.raises(FileNotFoundError):
            inference.append_detections(run, [_fd(2.0)])
        assert not (tmp_storage / "1" / "2").exists()
