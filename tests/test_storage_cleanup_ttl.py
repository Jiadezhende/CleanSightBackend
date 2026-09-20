"""TTL 回收的判据：`{task}/{step}/` 目录自身的 mtime，**对两种落盘布局一视同仁**。

守的是接线那一刻最容易单向漏盘的一条：HLS 写侧从 `persistence` 切到 `recording` 之后，
`metadata.json` 从 `{step}/` 平铺挪进了 `{step}/hls/`，旧判据 `glob("*/*/metadata.json")`
匹配不到新布局 → **老数据照常回收、新数据永不回收**，两端都不报错。

判据换成目录 mtime 后：

    平铺  {step}/raw_segment_*.mp4          ← 老数据，随 TTL 自然消失（「老数据不管」的前提）
    分域  {step}/hls/raw_segment_*.mp4      ← 新数据，同样被回收

外加两条偏差（是设计后果，不是 bug，见 `_scan_and_clean` 的 docstring）：
`{step}/lab/` 延迟创建会刷新 mtime 续命；活跃 step 不再免疫（本文件不造长跑 step，只钉前者）。
"""

import os
import time
from pathlib import Path

import pytest

from app.services.persistence.workers.cleanup_worker import StorageCleanupWorker

_DAY = 86400.0
_RETENTION_DAYS = 15


def _worker(db_dir: Path) -> StorageCleanupWorker:
    return StorageCleanupWorker(db_dir=db_dir, cleanup_days=_RETENTION_DAYS)


def _age(directory: Path, days: float) -> None:
    """把目录自身的 mtime 拨老 `days` 天（**在建完所有子项之后调**，否则会被子项创建刷新）。"""
    old = time.time() - days * _DAY
    os.utime(directory, (old, old))


def _make_flat_step(root: Path, task_id: int, step_id: int) -> Path:
    """老布局：段与 metadata 直接铺在 `{step}/` 下。"""
    step_dir = root / str(task_id) / str(step_id)
    step_dir.mkdir(parents=True)
    (step_dir / "raw_segment_1700000000.mp4").write_bytes(b"fake")
    (step_dir / "metadata.json").write_text("{}", encoding="utf-8")
    return step_dir


def _make_domain_step(root: Path, task_id: int, step_id: int) -> Path:
    """新布局：产物按域隔离在 `{step}/hls/` 下（metadata.json 也在里面）。"""
    step_dir = root / str(task_id) / str(step_id)
    hls_dir = step_dir / "hls"
    hls_dir.mkdir(parents=True)
    (hls_dir / "raw_segment_1700000000.mp4").write_bytes(b"fake")
    (hls_dir / "metadata.json").write_text("{}", encoding="utf-8")
    return step_dir


# --- 1. 两种布局的过期 step 都被删 ---

def test_expired_steps_of_both_layouts_are_deleted(tmp_path):
    flat = _make_flat_step(tmp_path, 1, 1)
    domain = _make_domain_step(tmp_path, 2, 1)
    _age(flat, _RETENTION_DAYS + 1)
    _age(domain, _RETENTION_DAYS + 1)

    assert _worker(tmp_path)._scan_and_clean() == 2

    assert not flat.exists()
    assert not domain.exists()
    # 被掏空的 task 目录顺手回收
    assert not (tmp_path / "1").exists()
    assert not (tmp_path / "2").exists()


# --- 2. 新鲜的 step 不动（两种布局同样） ---

@pytest.mark.parametrize("make_step", [_make_flat_step, _make_domain_step])
def test_fresh_step_survives(tmp_path, make_step):
    step_dir = make_step(tmp_path, 7, 3)
    _age(step_dir, _RETENTION_DAYS - 1)

    assert _worker(tmp_path)._scan_and_clean() == 0
    assert step_dir.exists()


# --- 3. 段落盘不续命：写段动的是 {step}/hls/ 的 mtime，{step}/ 纹丝不动 ---

def test_writing_a_segment_does_not_renew_the_step(tmp_path):
    """这正是「目录 mtime ≈ 创建时间」成立的原因，也是不能复用
    `tasks.list_task_ids(order="mtime")`（下钻取最大值 = 最近活动）的原因。"""
    step_dir = _make_domain_step(tmp_path, 5, 2)
    _age(step_dir, _RETENTION_DAYS + 1)

    # 新段落盘：只刷新域子目录的 mtime
    (step_dir / "hls" / "raw_segment_1800000000.mp4").write_bytes(b"fresh")

    assert _worker(tmp_path)._scan_and_clean() == 1
    assert not step_dir.exists()


# --- 4. 已知偏差：{step}/lab/ 延迟创建会刷新 {step}/ 的 mtime，TTL 计时重置 ---

def test_lazy_lab_dir_renews_the_step_ttl(tmp_path):
    """白捡的续命（正被反复导出的 step 天然不被回收），**不是 bug**。

    钉住它是为了：后人看见「某个 step 超过 cleanup_days 还在」时，能在这里找到解释，
    而不是当成漂移去查。
    """
    step_dir = _make_domain_step(tmp_path, 9, 4)
    _age(step_dir, _RETENTION_DAYS + 1)

    # 第一次被导出送标：lab 域目录此刻才建出来，作为直接子项的增删刷新 {step}/ 的 mtime
    (step_dir / "lab").mkdir()

    assert _worker(tmp_path)._scan_and_clean() == 0
    assert step_dir.exists()


# --- 5. 非 task 目录不进扫描范围（.lab_exports 靠自己的孤儿扫描，不归 TTL） ---

def test_non_numeric_dirs_are_not_touched(tmp_path):
    exports = tmp_path / ".lab_exports" / "clip_1"
    exports.mkdir(parents=True)
    (exports / "clip.mp4").write_bytes(b"fake")
    _age(exports, _RETENTION_DAYS + 10)
    _age(tmp_path / ".lab_exports", _RETENTION_DAYS + 10)

    assert _worker(tmp_path)._scan_and_clean() == 0
    assert exports.exists()


# --- 6. 布局不是本文件自己编的：用 storage.hls 的真实路径再钉一次 ---

def test_real_hls_domain_layout_is_covered(tmp_storage):
    """上面的 `_make_domain_step` 手写了 `hls/` 这一级。万一写侧哪天再下沉一级，手写的
    用例会继续绿而生产继续漏盘——故这里用 `hls.segment_path` 取真实落位再验一遍。"""
    from app.settings import settings
    from app.storage import hls

    seg = hls.segment_path(11, 6, hls.SegmentRef(track="raw", ts_us=1700000000))
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(b"fake")
    step_dir = seg.parent.parent
    assert step_dir == tmp_storage / "11" / "6"
    _age(step_dir, _RETENTION_DAYS + 1)

    assert _worker(settings.storage_base_dir)._scan_and_clean() == 1
    assert not step_dir.exists()


# --- 7. 空存储根 / 不存在的根：不抛 ---

def test_missing_root_is_a_no_op(tmp_path):
    assert _worker(tmp_path / "nope")._scan_and_clean() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
