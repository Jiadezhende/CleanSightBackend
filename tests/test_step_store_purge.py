"""
step 目录产物清单 / 活动时间（`step_store.products`）与删除入口
（`store.purge_step` / `store.sweep_empty_tasks`）的单元测试。

重点覆盖 TTL 判据改造的两个方向（此前 cleanup_worker 零测试覆盖）：
- **正向**：只有 features.jsonl 无 metadata.json 的目录必须能被看见（旧判据扫不到，
  是确定的泄漏路径）
- **反向**：崩溃残留的临时文件不得让死 step 显得活跃（否则永远躲过回收）
"""

import os
import time
from pathlib import Path

import pytest

from app.services.step_store import layout, products
from app.services.step_store import store as step_store

TASK_ID = 4242
STEP_ID = 7


def _touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def step_dir(tmp_storage) -> Path:
    d = tmp_storage / str(TASK_ID) / str(STEP_ID)
    d.mkdir(parents=True)
    return d


class TestListProducts:
    """`list_products` 是包内实现（门面不出产物清单，TTL 只要 last_activity_at）。"""

    def test_recognizes_all_registered_kinds(self, step_dir):
        expected = {
            layout.segment_name("raw", 1700000000000000),
            layout.segment_name("processed", 1700000000000000),
            layout.sidecar_name("raw", 1700000000000000),
            layout.init_name("raw"),
            layout.playlist_name("processed"),
            layout.METADATA_NAME,
            "features.jsonl",
            "facts.jsonl",
            "offline_inference_result.json",
            "visual_roi_v1.npz",
        }
        for name in expected:
            _touch(step_dir / name)

        got = set(products.list_products(step_dir))
        assert got == expected

    def test_ignores_reader_side_temp_files(self, step_dir):
        """读侧在 step 目录写临时 m3u8（clip_builder / step_exporter），崩溃会残留。

        它们若被当成产物，会让一个死 step 永远显得活跃、躲过 TTL 回收。
        """
        _touch(step_dir / ".clip_deadbeef.m3u8")
        _touch(step_dir / ".export_cafebabe.m3u8")
        _touch(step_dir / ".raw_segment_1.tmp_init.mp4")
        assert products.list_products(step_dir) == []

    def test_ignores_unregistered_files(self, step_dir):
        _touch(step_dir / "notes.txt")
        _touch(step_dir / "raw_segment_1.tmp")
        assert products.list_products(step_dir) == []

    def test_empty_dir(self, step_dir):
        assert products.list_products(step_dir) == []


class TestLastActivity:
    def test_none_when_no_products(self, step_dir):
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at is None

    def test_takes_max_mtime(self, step_dir):
        base = time.time() - 10000
        _touch(step_dir / layout.METADATA_NAME, base)
        _touch(step_dir / layout.segment_name("raw", 1), base + 500)
        _touch(step_dir / "features.jsonl", base + 200)
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at == pytest.approx(base + 500)

    def test_sees_inference_only_step(self, step_dir):
        """**P1-③ 正向验收**：只有 features.jsonl、没有任何 HLS 产物的 step。

        旧判据 glob("*/*/metadata.json") 扫不到这类目录 → 无限期堆积。
        """
        stamp = time.time() - 10000
        _touch(step_dir / "features.jsonl", stamp)
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at == pytest.approx(stamp)

    def test_temp_file_does_not_revive_dead_step(self, step_dir):
        """**反向验收**：死 step 里残留一个新鲜的临时文件，不得被判为活跃。"""
        old = time.time() - 10000
        _touch(step_dir / layout.METADATA_NAME, old)
        _touch(step_dir / ".export_fresh.m3u8")  # mtime = 现在
        assert step_store.step(TASK_ID, STEP_ID).last_activity_at == pytest.approx(old)

    def test_fresh_segment_keeps_stale_metadata_step_alive(self, step_dir):
        """**不误删活跃 step**：刚落新段但 metadata.json 是旧的。

        活跃 step 每 ~10s 落一个新段，故按产物 mtime 取 max 能识别活跃态。
        """
        old = time.time() - 10000
        _touch(step_dir / layout.METADATA_NAME, old)
        _touch(step_dir / layout.segment_name("raw", 2))  # mtime = 现在
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

    def test_missing_base_dir(self, tmp_path):
        assert list(products.iter_steps(tmp_path / "nope")) == []

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
