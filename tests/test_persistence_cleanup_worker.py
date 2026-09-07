"""
存储 TTL 清理 Worker 的单元测试（此前零覆盖）。

只测 `_scan_and_clean` 的判定与删除，不起线程——`start()/stop()` 是 daemon 线程
样板，值不上一个会 sleep 的用例。

判据于 2026-09 从 `metadata.json.updated_at` 换成「已登记产物的 mtime 最大值」，
本文件同时锁住换代前后都必须成立的行为与换代后新增的行为。
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


def _worker(**kw) -> StorageCleanupWorker:
    """存储根由 step_store 自解析；测试靠 `tmp_storage` monkeypatch settings。"""
    return StorageCleanupWorker(cleanup_days=DAYS, **kw)


class TestRetention:
    def test_deletes_expired_step(self, tmp_storage):
        step = tmp_storage / "1" / "2"
        _touch(step / layout.segment_name("raw", 1), age_days=DAYS + 1)
        _touch(step / layout.METADATA_NAME, age_days=DAYS + 1)

        assert _worker()._scan_and_clean() == 1
        assert not step.exists()

    def test_keeps_fresh_step(self, tmp_storage):
        step = tmp_storage / "1" / "2"
        _touch(step / layout.segment_name("raw", 1), age_days=1)

        assert _worker()._scan_and_clean() == 0
        assert step.exists()

    def test_boundary_just_inside_retention(self, tmp_storage):
        """恰好在保留期内（差一小时）不删。"""
        step = tmp_storage / "1" / "2"
        _touch(step / layout.METADATA_NAME, age_days=DAYS - 1 / 24)

        assert _worker()._scan_and_clean() == 0
        assert step.exists()

    def test_fresh_segment_saves_step_with_stale_metadata(self, tmp_storage):
        """**不误删活跃 step**：metadata.json 很旧但刚落了新段。

        换代前判据只看 metadata.json，这种目录会被误删。
        """
        step = tmp_storage / "1" / "2"
        _touch(step / layout.METADATA_NAME, age_days=DAYS + 5)
        _touch(step / layout.segment_name("raw", 9), age_days=0)

        assert _worker()._scan_and_clean() == 0
        assert step.exists()


class TestInferenceOnlyStep:
    """P1-③ 正向验收：只有 inference 产物、无 HLS 产物的 step。

    旧判据 glob("*/*/metadata.json") 完全扫不到这类目录 → features.jsonl 无限期堆积。
    """

    def test_deletes_expired_inference_only_step(self, tmp_storage):
        step = tmp_storage / "1" / "2"
        _touch(step / "features.jsonl", age_days=DAYS + 1)
        _touch(step / "facts.jsonl", age_days=DAYS + 1)

        assert _worker()._scan_and_clean() == 1
        assert not step.exists()

    def test_keeps_fresh_inference_only_step(self, tmp_storage):
        step = tmp_storage / "1" / "2"
        _touch(step / "features.jsonl", age_days=1)

        assert _worker()._scan_and_clean() == 0
        assert step.exists()


class TestSkips:
    def test_skips_step_without_registered_products(self, tmp_storage):
        """无任何已登记产物：判不出「还没写」还是「写完被清空」，不删。"""
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
        old = tmp_storage / "1" / "2"
        new = tmp_storage / "1" / "3"
        _touch(old / "features.jsonl", age_days=DAYS + 1)
        _touch(new / "features.jsonl", age_days=0)

        assert _worker()._scan_and_clean() == 1
        assert not old.exists()
        assert new.exists()


class TestEmptyTaskDirCleanup:
    def test_removes_task_dir_after_last_step_deleted(self, tmp_storage):
        _touch(tmp_storage / "1" / "2" / "features.jsonl", age_days=DAYS + 1)

        assert _worker()._scan_and_clean() == 1
        assert not (tmp_storage / "1").exists()

    def test_keeps_task_dir_with_surviving_step(self, tmp_storage):
        _touch(tmp_storage / "1" / "2" / "features.jsonl", age_days=DAYS + 1)
        _touch(tmp_storage / "1" / "3" / "features.jsonl", age_days=0)

        _worker()._scan_and_clean()
        assert (tmp_storage / "1").exists()
        assert (tmp_storage / "1" / "3").exists()


class TestDryRun:
    def test_counts_but_does_not_delete(self, tmp_storage):
        step = tmp_storage / "1" / "2"
        _touch(step / "features.jsonl", age_days=DAYS + 1)

        assert _worker(dry_run=True)._scan_and_clean() == 1
        assert step.exists(), "dry-run 必须保留目录"

    def test_does_not_touch_empty_task_dirs(self, tmp_storage):
        task = tmp_storage / "1"
        task.mkdir()

        _worker(dry_run=True)._scan_and_clean()
        assert task.exists()
