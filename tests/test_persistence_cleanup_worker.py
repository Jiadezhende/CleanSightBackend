"""
存储 TTL 清理 Worker 的单元测试（此前零覆盖）。

只测 `_scan_and_clean` 的判定与删除，不起线程——`start()/stop()` 是 daemon 线程
样板，值不上一个会 sleep 的用例。

判据于 2026-09 两次换代：`metadata.json.updated_at` →「已登记产物 mtime 最大值」→
step 目录内的**活动标记**（`layout.ACTIVITY_NAME`，由 step_store 写入口顺带刷新）。
本文件锁住三代判据下都必须成立的行为，以及最后这次换代新增的行为。
"""

import os
import time
from pathlib import Path

import pytest

from app.services.persistence.workers.cleanup_worker import StorageCleanupWorker
from app.services.step_store import layout

DAYS = 7
_DAY_S = 86400


def _touch(path: Path, age_days: float = 0.0) -> Path:
    """建文件并把 mtime 设成 age_days 天前。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = time.time() - age_days * _DAY_S
    os.utime(path, (stamp, stamp))
    return path


def _aged_step(step_dir: Path, age_days: float, *products: str) -> Path:
    """建一个「最后活动在 age_days 天前」的 step 目录。

    活动标记就是判据本身；`products` 只是让目录像真的——产物自身的 mtime 不参与判定，
    由 `test_stale_marker_ages_out_despite_fresh_products` 锁死。
    """
    for name in products:
        _touch(step_dir / name, age_days)
    _touch(step_dir / layout.ACTIVITY_NAME, age_days)
    return step_dir


def _worker(**kw) -> StorageCleanupWorker:
    """存储根由 step_store 自解析；测试靠 `tmp_storage` monkeypatch settings。"""
    return StorageCleanupWorker(cleanup_days=DAYS, **kw)


class TestRetention:
    def test_deletes_expired_step(self, tmp_storage):
        step = _aged_step(tmp_storage / "1" / "2", DAYS + 1,
                          layout.segment_name("raw", 1), layout.METADATA_NAME)

        assert _worker()._scan_and_clean() == 1
        assert not step.exists()

    def test_keeps_fresh_step(self, tmp_storage):
        step = _aged_step(tmp_storage / "1" / "2", 1, layout.segment_name("raw", 1))

        assert _worker()._scan_and_clean() == 0
        assert step.exists()

    def test_boundary_just_inside_retention(self, tmp_storage):
        """恰好在保留期内（差一小时）不删。"""
        step = _aged_step(tmp_storage / "1" / "2", DAYS - 1 / 24, layout.METADATA_NAME)

        assert _worker()._scan_and_clean() == 0
        assert step.exists()

    def test_stale_products_do_not_age_out_an_active_step(self, tmp_storage):
        """**不误删活跃 step**：产物文件很旧，但刚有人写过（标记新鲜）。

        第一代判据只看 metadata.json，这种目录会被误删。
        """
        step = tmp_storage / "1" / "2"
        _touch(step / layout.METADATA_NAME, age_days=DAYS + 5)
        _touch(step / layout.segment_name("raw", 9), age_days=DAYS + 5)
        _touch(step / layout.ACTIVITY_NAME, age_days=0)

        assert _worker()._scan_and_clean() == 0
        assert step.exists()

    def test_stale_marker_ages_out_despite_fresh_products(self, tmp_storage):
        """判据只认标记，没有第二个真源。

        写入口一定会刷标记，故「产物新而标记旧」在生产里不出现；本例锁的是判据的单一性
        —— 有第二个真源，崩溃残留的新文件就又能让死 step 赖着不走（第二代判据的坑）。
        """
        step = tmp_storage / "1" / "2"
        _touch(step / layout.segment_name("raw", 9), age_days=0)
        _touch(step / layout.ACTIVITY_NAME, age_days=DAYS + 1)

        assert _worker()._scan_and_clean() == 1
        assert not step.exists()


class TestInferenceOnlyStep:
    """P1-③ 正向验收：只有 inference 产物、无 HLS 产物的 step。

    第一代判据 glob("*/*/metadata.json") 完全扫不到这类目录 → features.jsonl 无限期堆积。
    """

    def test_deletes_expired_inference_only_step(self, tmp_storage):
        step = _aged_step(tmp_storage / "1" / "2", DAYS + 1,
                          "features.jsonl", "facts.jsonl")

        assert _worker()._scan_and_clean() == 1
        assert not step.exists()

    def test_keeps_fresh_inference_only_step(self, tmp_storage):
        step = _aged_step(tmp_storage / "1" / "2", 1, "features.jsonl")

        assert _worker()._scan_and_clean() == 0
        assert step.exists()


class TestSkips:
    def test_skips_step_without_activity_marker(self, tmp_storage):
        """无活动标记：判不出「还没写」「写完被清空」还是「判据上线前的历史目录」，不删。"""
        step = tmp_storage / "1" / "2"
        step.mkdir(parents=True)
        _touch(step / ".export_leftover.m3u8", age_days=DAYS + 1)

        assert _worker()._scan_and_clean() == 0
        assert step.exists()

    def test_skips_non_numeric_dirs(self, tmp_storage):
        exports = tmp_storage / ".lab_exports"
        _touch(exports / "step_1_2_raw_x.mp4", age_days=DAYS + 10)

        assert _worker()._scan_and_clean() == 0
        assert exports.exists()

    def test_leaves_fresh_sibling_untouched(self, tmp_storage):
        old = _aged_step(tmp_storage / "1" / "2", DAYS + 1, "features.jsonl")
        new = _aged_step(tmp_storage / "1" / "3", 0, "features.jsonl")

        assert _worker()._scan_and_clean() == 1
        assert not old.exists()
        assert new.exists()


class TestEmptyTaskDirCleanup:
    def test_removes_task_dir_after_last_step_deleted(self, tmp_storage):
        _aged_step(tmp_storage / "1" / "2", DAYS + 1, "features.jsonl")

        assert _worker()._scan_and_clean() == 1
        assert not (tmp_storage / "1").exists()

    def test_keeps_task_dir_with_surviving_step(self, tmp_storage):
        _aged_step(tmp_storage / "1" / "2", DAYS + 1, "features.jsonl")
        _aged_step(tmp_storage / "1" / "3", 0, "features.jsonl")

        _worker()._scan_and_clean()
        assert (tmp_storage / "1").exists()
        assert (tmp_storage / "1" / "3").exists()


class TestDryRun:
    def test_counts_but_does_not_delete(self, tmp_storage):
        step = _aged_step(tmp_storage / "1" / "2", DAYS + 1, "features.jsonl")

        assert _worker(dry_run=True)._scan_and_clean() == 1
        assert step.exists(), "dry-run 必须保留目录"

    def test_does_not_touch_empty_task_dirs(self, tmp_storage):
        task = tmp_storage / "1"
        task.mkdir()

        _worker(dry_run=True)._scan_and_clean()
        assert task.exists()
