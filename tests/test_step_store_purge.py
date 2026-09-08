"""
step 目录的活动时间（`Step.last_activity_at`）与删除入口
（`store.purge_step` / `store.sweep_empty_tasks`）的单元测试。

TTL 判据是 step 目录里的活动标记（`layout.ACTIVITY_NAME`），由 step_store 的写入口顺带
刷新。本文件锁住的是判据本身的两个方向：

- **正向**：只有 inference 产物、无任何 HLS 段的 step 必须能被看见并按时回收
  （最早那版判据 `glob("*/*/metadata.json")` 扫不到这类目录，是确定的泄漏路径）
- **反向**：崩溃残留的临时文件不得让死 step 显得活跃（否则永远躲过回收）

「写的时候真的刷了标记吗」由 tests/test_step_store_api.py::TestActivityMarker 锁。
"""

import os
import time
from pathlib import Path

import pytest

from app.services.step_store import layout
from app.services.step_store import store as step_store

TASK_ID = 4242
STEP_ID = 7


def _touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _mark_active(step_dir: Path, mtime: float | None = None) -> Path:
    """按写入口的效果落一个活动标记（写入口的真实行为由 TestActivityMarker 锁）。"""
    return _touch(step_dir / layout.ACTIVITY_NAME, mtime)


@pytest.fixture
def step_dir(tmp_storage) -> Path:
    d = tmp_storage / str(TASK_ID) / str(STEP_ID)
    d.mkdir(parents=True)
    return d


class TestLastActivity:
    def test_none_without_marker(self, step_dir):
        """标记不存在 = 判不出死活（刚建目录 / 判据上线前的历史目录），交调用方决定。"""
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at is None

    def test_none_for_missing_dir(self, tmp_storage):
        assert step_store.step(1, 2).last_activity_at is None

    def test_reads_marker_mtime(self, step_dir):
        stamp = time.time() - 10000
        _mark_active(step_dir, stamp)
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at == pytest.approx(stamp)

    def test_sees_inference_only_step(self, step_dir):
        """**正向验收**：只有 features.jsonl、没有任何 HLS 产物的 step 照样有活动时间。

        最早那版判据 glob("*/*/metadata.json") 扫不到这类目录 → 无限期堆积。
        """
        stamp = time.time() - 10000
        _touch(step_dir / "features.jsonl", stamp)
        _mark_active(step_dir, stamp)
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at == pytest.approx(stamp)

    def test_temp_file_does_not_revive_dead_step(self, step_dir):
        """**反向验收**：死 step 里残留一个新鲜的临时文件，不得被判为活跃。

        临时文件不经写入口，刷不到标记；`scratch_path` 也刻意不刷。
        """
        old = time.time() - 10000
        _mark_active(step_dir, old)
        _touch(step_dir / ".export_fresh.m3u8")  # mtime = 现在
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at == pytest.approx(old)

    def test_stale_product_does_not_age_out_active_step(self, step_dir):
        """**不误删活跃 step**：产物文件很旧但刚有人写过（标记新鲜）。"""
        old = time.time() - 10000
        _touch(step_dir / layout.METADATA_NAME, old)
        _mark_active(step_dir)  # mtime = 现在
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at > old + 1000


class TestIterSteps:
    """出的是 (task_id, step_id) 而非 Path —— 目录结构不漏给调用方。"""

    def test_yields_id_pairs(self, tmp_storage):
        (tmp_storage / "1" / "2").mkdir(parents=True)
        (tmp_storage / "1" / "3").mkdir(parents=True)
        (tmp_storage / "10" / "0").mkdir(parents=True)
        assert {(s.task_id, s.step_id) for s in step_store.steps(include_empty=True)} == {(1, 2), (1, 3), (10, 0)}

    def test_skips_non_numeric_dirs(self, tmp_storage):
        (tmp_storage / ".lab_exports" / "5").mkdir(parents=True)
        (tmp_storage / "7" / "notastep").mkdir(parents=True)
        (tmp_storage / "7" / "1").mkdir(parents=True)
        assert [(s.task_id, s.step_id) for s in step_store.steps(include_empty=True)] == [(7, 1)]

    def test_skips_files_at_task_level(self, tmp_storage):
        _touch(tmp_storage / "lab_runtime_config.json")
        assert [(s.task_id, s.step_id) for s in step_store.steps(include_empty=True)] == []

    def test_missing_base_dir(self, tmp_path, monkeypatch):
        """存储根整个不存在（首次启动、盘没挂上）：枚举返回空而不是抛。"""
        from app.settings import settings

        monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "nope"))
        assert step_store.steps(include_empty=True) == []

    def test_sees_step_without_any_hls_product(self, tmp_storage):
        """与 steps() 默认语义的差异：那个按契约丢弃无段 step，include_empty 必须看见。"""
        _touch(tmp_storage / "1" / "2" / "features.jsonl")
        assert [(s.task_id, s.step_id) for s in step_store.steps(include_empty=True)] == [(1, 2)]


class TestPurgeStep:
    def test_removes_all_services_products(self, step_dir):
        """删的是整个目录，含 inference 写的产物——不只调用方自己的。"""
        _touch(step_dir / layout.segment_name("raw", 1))
        _touch(step_dir / "features.jsonl")
        _touch(step_dir / "facts.jsonl")
        assert step_store.purge_step(TASK_ID, STEP_ID) is True
        assert not step_dir.exists()

    def test_returns_false_when_absent(self, tmp_storage):
        assert step_store.purge_step(1, 2) is False

    def test_leaves_sibling_steps_untouched(self, tmp_storage):
        _touch(tmp_storage / "1" / "2" / "features.jsonl")
        _touch(tmp_storage / "1" / "3" / "features.jsonl")
        step_store.purge_step(1, 2)
        assert not (tmp_storage / "1" / "2").exists()
        assert (tmp_storage / "1" / "3" / "features.jsonl").exists()


class TestSweepEmptyTasks:
    def test_removes_empty_task_dirs(self, tmp_storage):
        (tmp_storage / "1").mkdir()
        (tmp_storage / "2").mkdir()
        assert step_store.sweep_empty_tasks() == 2
        assert not (tmp_storage / "1").exists()

    def test_keeps_task_dir_with_steps(self, tmp_storage):
        (tmp_storage / "1" / "2").mkdir(parents=True)
        assert step_store.sweep_empty_tasks() == 0
        assert (tmp_storage / "1").exists()

    def test_ignores_non_numeric_dirs(self, tmp_storage):
        (tmp_storage / ".lab_exports").mkdir()
        assert step_store.sweep_empty_tasks() == 0
        assert (tmp_storage / ".lab_exports").exists()
