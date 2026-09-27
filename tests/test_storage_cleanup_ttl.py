"""TTL 回收：判据是 `{task}/{step}/` 目录自身的 mtime，整个 step（含所有 run）一起删。

    {step}/{run_id}/hls/ …   现行布局：`{step}` 的直接子项只有 run 目录
    {step}/hls/ …            旧布局残留：同一判据，随 step 回收

step mtime 只在增删直接子项时变，所以它 = 最近一次 `runs.allocate` 的时刻：写段不续命，
开新 run 续命。删除经 `_fs.remove`（先 rename 进 `.trash/`），回收区每轮先清空。
"""

import os
import time
from pathlib import Path

import pytest

from app.services.persistence.workers.cleanup_worker import StorageCleanupWorker
from app.storage import _fs

_DAY = 86400.0
_RETENTION_DAYS = 15


def _worker(db_dir: Path) -> StorageCleanupWorker:
    return StorageCleanupWorker(db_dir=db_dir, cleanup_days=_RETENTION_DAYS)


def _age(directory: Path, days: float) -> None:
    """把目录自身的 mtime 拨老 `days` 天（**在建完所有子项之后调**，否则会被子项创建刷新）。"""
    old = time.time() - days * _DAY
    os.utime(directory, (old, old))


def _make_run_step(root: Path, task_id: int, step_id: int, run_id: int = 1700) -> Path:
    """现行布局：`{step}/{run_id}/hls/` 下一段 + metadata。返回 step 目录。"""
    hls_dir = root / str(task_id) / str(step_id) / str(run_id) / "hls"
    hls_dir.mkdir(parents=True)
    (hls_dir / "raw_segment_1700000000.mp4").write_bytes(b"fake")
    (hls_dir / "metadata.json").write_text("{}", encoding="utf-8")
    return hls_dir.parent.parent


def _make_legacy_step(root: Path, task_id: int, step_id: int) -> Path:
    """旧布局残留：`{step}/hls/`。返回 step 目录。"""
    hls_dir = root / str(task_id) / str(step_id) / "hls"
    hls_dir.mkdir(parents=True)
    (hls_dir / "raw_segment_1700000000.mp4").write_bytes(b"fake")
    return hls_dir.parent


# --- 1. 过期的 step 整个删掉（两种布局），被掏空的 task 目录顺手回收 ---

def test_expired_steps_are_deleted_whole(tmp_path):
    current = _make_run_step(tmp_path, 1, 1)
    (current / "1800").mkdir()                     # 同 step 的另一个 run 一起删
    legacy = _make_legacy_step(tmp_path, 2, 1)
    _age(current, _RETENTION_DAYS + 1)
    _age(legacy, _RETENTION_DAYS + 1)

    assert _worker(tmp_path)._scan_and_clean() == 2

    assert not current.exists()
    assert not legacy.exists()
    assert not (tmp_path / "1").exists()
    assert not (tmp_path / "2").exists()


# --- 2. 新鲜的 step 不动 ---

@pytest.mark.parametrize("make_step", [_make_run_step, _make_legacy_step])
def test_fresh_step_survives(tmp_path, make_step):
    step_dir = make_step(tmp_path, 7, 3)
    _age(step_dir, _RETENTION_DAYS - 1)

    assert _worker(tmp_path)._scan_and_clean() == 0
    assert step_dir.exists()


# --- 3. 写段不续命、开新 run 续命 ---

def test_writing_into_a_run_does_not_renew_the_step(tmp_path):
    """段落在 `{step}/{run}/hls/`，`{step}` 的 mtime 纹丝不动。"""
    step_dir = _make_run_step(tmp_path, 5, 2)
    _age(step_dir, _RETENTION_DAYS + 1)

    (step_dir / "1700" / "hls" / "raw_segment_1800000000.mp4").write_bytes(b"fresh")

    assert _worker(tmp_path)._scan_and_clean() == 1
    assert not step_dir.exists()


def test_allocating_a_new_run_renews_the_step(tmp_storage):
    """TTL 从该 step 最后一次开跑算起：新 run 目录是 `{step}` 的直接子项，刷新它的 mtime。"""
    from app.storage import runs

    step_dir = _make_run_step(tmp_storage, 9, 4)
    _age(step_dir, _RETENTION_DAYS + 1)

    runs.allocate(9, 4)

    assert _worker(tmp_storage)._scan_and_clean() == 0
    assert step_dir.exists()


# --- 4. 非 task 目录不进扫描范围，也不被当成空 task 目录删 ---

def test_non_numeric_dirs_are_not_touched(tmp_path):
    exports = tmp_path / ".lab_exports" / "clip_1"
    exports.mkdir(parents=True)
    (exports / "clip.mp4").write_bytes(b"fake")
    _age(exports, _RETENTION_DAYS + 10)
    _age(tmp_path / ".lab_exports", _RETENTION_DAYS + 10)

    assert _worker(tmp_path)._scan_and_clean() == 0
    assert exports.exists()


def test_empty_non_numeric_dirs_are_not_rmdired(tmp_path):
    (tmp_path / ".lab_exports").mkdir()
    (tmp_path / "3").mkdir()

    _worker(tmp_path)._scan_and_clean()

    assert (tmp_path / ".lab_exports").is_dir()
    assert not (tmp_path / "3").exists()


# --- 5. 回收区：每轮先清空；rename 失败时 step 原样不动 ---

def test_each_round_purges_trash_first(tmp_path):
    leftover = tmp_path / _fs.TRASH_NAME / "deadbeef" / "hls"
    leftover.mkdir(parents=True)
    (leftover / "seg.mp4").write_bytes(b"x")

    _worker(tmp_path)._scan_and_clean()

    assert list((tmp_path / _fs.TRASH_NAME).iterdir()) == []


def test_failed_rename_keeps_the_step_intact(tmp_path, monkeypatch):
    step_dir = _make_run_step(tmp_path, 4, 1)
    _age(step_dir, _RETENTION_DAYS + 1)

    def boom(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(_fs.os, "rename", boom)
    assert _worker(tmp_path)._scan_and_clean() == 0
    assert (step_dir / "1700" / "hls" / "metadata.json").exists()


# --- 6. 布局不是本文件自己编的：用 runs + hls 的真实落位再钉一次 ---

def test_real_run_layout_is_covered(tmp_storage):
    """上面手写了 `{step}/{run}/hls/`。万一写侧哪天再下沉一级，手写的用例会继续绿而生产漏盘
    ——故这里用 `runs.allocate` + `hls.segment_path` 取真实落位再验一遍。"""
    from app.settings import settings
    from app.storage import hls, runs

    run = runs.allocate(11, 6)
    seg = hls.segment_path(run, hls.SegmentRef(track="raw", ts_us=1700000000), create=True)
    seg.write_bytes(b"fake")
    step_dir = tmp_storage / "11" / "6"
    assert seg.parent.parent.parent == step_dir
    _age(step_dir, _RETENTION_DAYS + 1)

    assert _worker(settings.storage_base_dir)._scan_and_clean() == 1
    assert not step_dir.exists()


# --- 7. 空存储根 / 不存在的根：不抛 ---

def test_missing_root_is_a_no_op(tmp_path):
    assert _worker(tmp_path / "nope")._scan_and_clean() == 0
