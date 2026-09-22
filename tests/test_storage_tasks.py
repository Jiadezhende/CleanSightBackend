"""`app.storage` 基础能力：`_root` 的根解析与 `tasks` 的定位/枚举/删除。

落盘约定：`{storage_root}/{task_id}/{step_id}/`，两级目录名均为十进制 id。
本文件全程用 `tmp_storage` fixture（conftest）把存储根指到临时目录，不碰真实 `database/`。

本包只负责「名字与定位」，故这里断言的全是路径与目录事实——不涉及任何产物的内容格式。
"""

import os
from pathlib import Path

import pytest

from app.storage import _root, tasks
from app.settings import settings


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------


def _seed_step(
    root: Path, task_id: int, step_id: int, domain: str = "hls", *filenames: str
) -> Path:
    """建 `{root}/{task_id}/{step_id}/{domain}/` 并放几个空文件，返回 **step 目录**。"""
    domain_dir = root / str(task_id) / str(step_id) / domain
    domain_dir.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        (domain_dir / name).write_bytes(b"x")
    return domain_dir.parent


def _set_mtime(step_dir: Path, ts: float) -> None:
    """把 step 目录**及其所有域子目录**的 mtime 钉到 ts。

    两层都要钉：`ids(order="mtime")` 取的是两者的最大值（产物写进 `hls/` 只更新域目录，
    step 目录不动），只钉一层会被另一层的"当前时刻"盖过去。
    """
    for target in (step_dir, *step_dir.iterdir()):
        if target.is_dir():
            os.utime(target, (ts, ts))


# ---------------------------------------------------------------------------
# _root：根解析
# ---------------------------------------------------------------------------


class TestRootPath:
    """`_root.path()` 是唯一定位入口，三个参数从左到右逐级下钻。"""

    def test_descends_level_by_level(self, tmp_storage):
        root = tmp_storage.resolve()
        assert _root.path() == root == settings.storage_base_dir
        assert _root.path(7) == root / "7"
        assert _root.path(7, 3) == root / "7" / "3"
        assert _root.path(7, 3, "hls") == root / "7" / "3" / "hls"

    def test_does_not_touch_disk(self, tmp_storage):
        _root.path(7, 3, "hls")
        assert not (tmp_storage / "7").exists()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"step_id": 3},                      # 跳过 task_id
            {"domain": "hls"},                   # 跳过 task_id + step_id
            {"task_id": 7, "domain": "hls"},     # 跳过 step_id
        ],
    )
    def test_skipping_a_level_raises(self, tmp_storage, kwargs):
        """跳级是编程错误：拼出来的路径会静默少一层，产物落到上一级目录。"""
        with pytest.raises(ValueError):
            _root.path(**kwargs)

    @pytest.mark.parametrize("domain", _root.DOMAINS)
    def test_whitelisted_domains_pass(self, tmp_storage, domain):
        assert _root.path(1, 2, domain).name == domain

    @pytest.mark.parametrize("domain", ["feature", "HLS", "hls/", "..", ""])
    def test_unknown_domain_raises(self, tmp_storage, domain):
        """域名笔误会静默造出第四个子目录：写侧不报错、读侧只是查不到、purge 照样删掉，
        连残留证据都不留。白名单把这类静默失败变成 ValueError。"""
        with pytest.raises(ValueError, match="Unknown domain"):
            _root.path(1, 2, domain)

    def test_unknown_domain_creates_nothing(self, tmp_storage):
        """校验必须早于 mkdir —— 否则非法域目录已经落盘了才报错。"""
        with pytest.raises(ValueError):
            _root.path(1, 2, "feature", create=True)
        assert list(tmp_storage.iterdir()) == []

    def test_create_false_leaves_disk_untouched(self, tmp_storage):
        """读一个不存在的 step 不该在盘上留空目录——空目录会被 ids() 列出却没有内容。"""
        _root.path(1, 2, "hls")
        assert list(tmp_storage.iterdir()) == []

    def test_create_true_makes_parents(self, tmp_storage):
        got = _root.path(1, 2, "hls", create=True)
        assert got.is_dir()
        assert (tmp_storage / "1" / "2").is_dir()  # 中间两层一并建出

    def test_create_true_is_idempotent(self, tmp_storage):
        _root.path(1, 2, "hls", create=True)
        _root.path(1, 2, "hls", create=True)  # exist_ok，不抛
        assert _root.path(1, 2, "hls").is_dir()

    def test_create_true_only_makes_its_own_domain(self, tmp_storage):
        """建 hls/ 不该顺带建出 features/ —— 域目录按需生成，空域不留痕。"""
        _root.path(1, 2, "hls", create=True)
        assert [p.name for p in (tmp_storage / "1" / "2").iterdir()] == ["hls"]

    def test_create_true_on_shallower_level(self, tmp_storage):
        """省掉 domain 也能建——那是 tasks.py 的用法，建的是 step 目录本身。"""
        got = _root.path(1, 2, create=True)
        assert got.is_dir() and got.name == "2"

    def test_domains_do_not_collide(self, tmp_storage):
        """同名文件落在不同域下互不干扰 —— 这就是隔离本身。"""
        hls = _root.path(1, 2, "hls", create=True) / "metadata.json"
        lab = _root.path(1, 2, "lab", create=True) / "metadata.json"
        assert hls != lab
        assert hls.parent.name == "hls" and lab.parent.name == "lab"

    @pytest.mark.parametrize(
        "name, expected",
        [("7", 7), ("0", 0), (".lab_exports", None), ("abc", None), ("", None)],
    )
    def test_dir_name_to_int(self, name, expected):
        assert _root.dir_name_to_int(name) == expected

    def test_cache_invalidates_on_settings_change(self, tmp_path, monkeypatch):
        """记忆化以 settings.storage_dir 原始值为 key —— patch 后必须立即生效。

        这条守的是「不能把 root 写成模块级常量」：写成常量则 import 期定死，
        conftest 的 tmp_storage 与本用例都会失效，而失效的表现是测试去写真实 database/。
        """
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()

        monkeypatch.setattr(settings, "storage_dir", str(first))
        assert _root.path() == first.resolve()

        monkeypatch.setattr(settings, "storage_dir", str(second))
        assert _root.path() == second.resolve()


# ---------------------------------------------------------------------------
# tasks.steps / tasks.ids：枚举
# ---------------------------------------------------------------------------


class TestSteps:
    def test_sorted_ascending(self, tmp_storage):
        for step_id in (3, 1, 10, 2):
            _seed_step(tmp_storage, 1, step_id)
        assert tasks.list_step_ids(1) == [1, 2, 3, 10]

    def test_skips_non_id_dirs_and_files(self, tmp_storage):
        _seed_step(tmp_storage, 1, 1)
        (tmp_storage / "1" / "scratch").mkdir()
        (tmp_storage / "1" / "notes.txt").write_text("x", encoding="utf-8")
        assert tasks.list_step_ids(1) == [1]

    def test_missing_task_dir_returns_empty(self, tmp_storage):
        assert tasks.list_step_ids(999) == []

    def test_does_not_judge_emptiness(self, tmp_storage):
        """空 step 目录必须被列出 —— TTL 要看见它（features.jsonl 泄漏的正是这一类）。

        「两轨都没段算不算数」是 HLS 域知识，本域不做这个判断。
        """
        (tmp_storage / "1" / "5").mkdir(parents=True)
        assert tasks.list_step_ids(1) == [5]


class TestIds:
    def test_sorted_ascending(self, tmp_storage):
        for task_id in (30, 1, 200):
            _seed_step(tmp_storage, task_id, 1)
        assert tasks.list_task_ids() == [1, 30, 200]

    def test_skips_non_id_entries(self, tmp_storage):
        """存储根下正常只有数字 task 目录（lab 产物已归入 {task}/{step}/lab/、
        LS 配置已移出存储根）。误建的目录与外部工具留下的文件一律跳过，不报错。"""
        _seed_step(tmp_storage, 1, 1)
        (tmp_storage / ".lab_exports").mkdir()  # 旧布局残留
        (tmp_storage / "lab_runtime_config.json").write_text("{}", encoding="utf-8")
        assert tasks.list_task_ids() == [1]

    def test_missing_root_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "nope"))
        assert tasks.list_task_ids() == []
        assert tasks.list_task_ids(order="mtime") == []

    def test_mtime_order_is_descending(self, tmp_storage):
        _set_mtime(_seed_step(tmp_storage, 1, 1), 1_700_000_100)
        _set_mtime(_seed_step(tmp_storage, 2, 1), 1_700_000_900)
        assert tasks.list_task_ids(order="mtime") == [2, 1]

    def test_mtime_follows_domain_subdir_not_just_step_dir(self, tmp_storage):
        """产物写进 `{step}/{domain}/` 只更新域目录的 mtime，step 目录纹丝不动。

        这条盯的是目录隔离的连带退化：若排序只 stat step 目录，该值会退化成「首次落盘
        时刻」，一个刚写过新段的 task 会被排到后面，大屏历史的「最近」就失准了。
        """
        stale = _seed_step(tmp_storage, 1, 1)
        fresh = _seed_step(tmp_storage, 2, 1)
        _set_mtime(stale, 1_700_000_100)
        _set_mtime(fresh, 1_700_000_100)  # 两个 step 目录本身同龄

        # 只让 task 2 的 hls/ 域目录"刚写过东西"——step 目录不碰
        os.utime(fresh / "hls", (1_700_000_900, 1_700_000_900))
        assert tasks.list_task_ids(order="mtime") == [2, 1]

    def test_mtime_order_keeps_task_without_steps_last(self, tmp_storage):
        """无 step 子目录的 task 排序键取 0（排最后），但**仍保留在结果里**——
        它是否该丢弃由调用方深扫时决定，不是本域的判断。"""
        _set_mtime(_seed_step(tmp_storage, 1, 1), 1_700_000_100)
        (tmp_storage / "2").mkdir()
        assert tasks.list_task_ids(order="mtime") == [1, 2]

    def test_mtime_tie_breaks_on_larger_task_id(self, tmp_storage):
        for task_id in (1, 2):
            _set_mtime(_seed_step(tmp_storage, task_id, 1), 1_700_000_100)
        assert tasks.list_task_ids(order="mtime") == [2, 1]

    def test_invalid_order_raises(self, tmp_storage):
        """传错 order 说明调用方对返回顺序有预期，静默按默认走比报错更坏。"""
        with pytest.raises(ValueError, match="Invalid order"):
            tasks.list_task_ids(order="recency")


# ---------------------------------------------------------------------------
# tasks.delete_step：删除
# ---------------------------------------------------------------------------


class TestPurgeStep:
    def test_removes_existing_step(self, tmp_storage):
        _seed_step(tmp_storage, 1, 1, "hls", "raw_segment_100.mp4")
        assert tasks.delete_step(1, 1) is True
        assert not (tmp_storage / "1" / "1").exists()

    def test_missing_step_returns_false(self, tmp_storage):
        assert tasks.delete_step(1, 1) is False

    def test_removes_every_domain_not_just_hls(self, tmp_storage):
        """它删的是整个 step，三个域一起没 —— 不是只删 hls/。

        历史上 hls_strategy.purge_step_dir 自述"只删 HLS 产物"而实际 rmtree 整个目录，
        这条用例把真实行为钉死，免得下一个读 docstring 的人再被误导。
        """
        _seed_step(tmp_storage, 1, 1, "hls", "raw_segment_100.mp4", "raw_playlist.m3u8")
        _seed_step(tmp_storage, 1, 1, "inference", "features.jsonl", "facts.jsonl")
        _seed_step(tmp_storage, 1, 1, "lab", "clip_1700_1710.mp4")

        assert tasks.delete_step(1, 1) is True
        assert not (tmp_storage / "1" / "1").exists()

    def test_reclaims_task_dir_when_last_step_removed(self, tmp_storage):
        _seed_step(tmp_storage, 1, 1)
        assert tasks.delete_step(1, 1) is True
        assert not (tmp_storage / "1").exists()

    def test_keeps_task_dir_when_other_steps_remain(self, tmp_storage):
        _seed_step(tmp_storage, 1, 1)
        _seed_step(tmp_storage, 1, 2)
        assert tasks.delete_step(1, 1) is True
        assert (tmp_storage / "1").is_dir()
        assert tasks.list_step_ids(1) == [2]

    def test_keeps_task_dir_with_non_step_leftovers(self, tmp_storage):
        """task 目录里若还有别的东西（非 step 目录/文件），rmdir 安全失败，目录保留。"""
        _seed_step(tmp_storage, 1, 1)
        (tmp_storage / "1" / "notes.txt").write_text("x", encoding="utf-8")
        assert tasks.delete_step(1, 1) is True
        assert (tmp_storage / "1").is_dir()
