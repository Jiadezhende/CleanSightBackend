"""`app.storage` 基础能力：`utils.root` 的根解析与 `tasks` 的定位/枚举。

落盘约定：`{storage_root}/{task_id}/{step_id}/`，两级目录名均为十进制 id。
本文件全程用 `tmp_storage` fixture（conftest）把存储根指到临时目录，不碰真实 `database/`。

本包只负责「名字与定位」，故这里断言的全是路径与目录事实——不涉及任何产物的内容格式。
"""

from pathlib import Path

import pytest

from app.types.run import RunIdentity
from app.storage import tasks
from app.storage.utils import root as _root
from app.settings import settings


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------


def _seed_run(root: Path, task_id: int, step_id: int, run_id: int) -> Path:
    """建 `{root}/{task_id}/{step_id}/{run_id}/`，返回 run 目录。"""
    run_dir = root / str(task_id) / str(step_id) / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


# ---------------------------------------------------------------------------
# _root：根解析
# ---------------------------------------------------------------------------


class TestRootPath:
    """`_root.path()` 逐级定位 task / step；run 以下归 `run_path` / `domain_dir`。"""

    def test_descends_level_by_level(self, tmp_storage):
        root = tmp_storage.resolve()
        assert _root.path() == root == settings.storage_base_dir
        assert _root.path(7) == root / "7"
        assert _root.path(7, 3) == root / "7" / "3"
        run = RunIdentity(7, 3, 11)
        assert _root.run_path(run) == root / "7" / "3" / "11"
        assert _root.run_path(run, "hls") == root / "7" / "3" / "11" / "hls"

    def test_does_not_touch_disk(self, tmp_storage):
        """定位不建目录——空目录会被枚举列出却没有内容。"""
        _root.path(7, 3)
        _root.run_path(RunIdentity(7, 3, 11), "hls")
        _root.domain_dir(RunIdentity(7, 3, 11), "hls")
        assert list(tmp_storage.iterdir()) == []

    def test_skipping_a_level_raises(self, tmp_storage):
        """跳级是编程错误：拼出来的路径会静默少一层。"""
        with pytest.raises(ValueError):
            _root.path(step_id=3)

    @pytest.mark.parametrize("domain", _root.DOMAINS)
    def test_whitelisted_domains_pass(self, tmp_storage, domain):
        assert _root.run_path(RunIdentity(1, 2, 3), domain).name == domain

    @pytest.mark.parametrize("domain", ["feature", "HLS", "hls/", "..", ""])
    def test_unknown_domain_raises(self, tmp_storage, domain):
        """域名笔误会静默造出第四个子目录：写侧不报错、读侧只是查不到。白名单把它变成 ValueError。"""
        with pytest.raises(ValueError, match="Unknown domain"):
            _root.run_path(RunIdentity(1, 2, 3), domain)

    def test_unknown_domain_creates_nothing(self, tmp_storage):
        """校验必须早于 mkdir —— 否则非法域目录已经落盘了才报错。"""
        (tmp_storage / "1" / "2" / "3").mkdir(parents=True)
        with pytest.raises(ValueError):
            _root.domain_dir(RunIdentity(1, 2, 3), "feature", create=True)
        assert list((tmp_storage / "1" / "2" / "3").iterdir()) == []

    def test_create_only_makes_the_domain_level(self, tmp_storage):
        """写者不建 run 目录：run 目录不在即 FileNotFoundError，什么都不建。"""
        with pytest.raises(FileNotFoundError):
            _root.domain_dir(RunIdentity(1, 2, 3), "hls", create=True)
        assert list(tmp_storage.iterdir()) == []

    def test_create_is_idempotent_and_only_makes_its_own_domain(self, tmp_storage):
        run_dir = tmp_storage / "1" / "2" / "3"
        run_dir.mkdir(parents=True)
        _root.domain_dir(RunIdentity(1, 2, 3), "hls", create=True)
        _root.domain_dir(RunIdentity(1, 2, 3), "hls", create=True)  # exist_ok，不抛
        assert [p.name for p in run_dir.iterdir()] == ["hls"]

    def test_domain_dir_rejects_non_run(self, tmp_storage):
        with pytest.raises(TypeError):
            _root.domain_dir((1, 2), "hls")

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

    def test_relative_storage_dir_resolves_to_project_root_regardless_of_cwd(
        self, tmp_path, monkeypatch
    ):
        """相对路径以项目根为基、不随进程 cwd 飘——否则读写两侧会分叉到不同目录。"""
        from app.services.persistence.config import get_persistence_config

        monkeypatch.setattr(settings, "storage_dir", "./database")
        resolved = _root.path()
        assert resolved.is_absolute() and resolved.name == "database"
        assert resolved == settings.storage_base_dir

        monkeypatch.chdir(tmp_path)                  # 切到完全无关的 cwd
        assert _root.path() == resolved
        assert tmp_path not in resolved.parents

        # TTL 清理（cleanup_worker）的扫描根取自这里，须与本包同源
        assert get_persistence_config().storage_base_dir == resolved

    def test_absolute_storage_dir_is_used_as_is(self, tmp_path, monkeypatch):
        abs_dir = tmp_path / "custom" / "store"
        monkeypatch.setattr(settings, "storage_dir", str(abs_dir))
        assert _root.path() == abs_dir.resolve()


# ---------------------------------------------------------------------------
# list_step_ids / list_task_ids：枚举
# ---------------------------------------------------------------------------


class TestSteps:
    def test_sorted_ascending(self, tmp_storage):
        for step_id in (3, 1, 10, 2):
            _seed_run(tmp_storage, 1, step_id, 1)
        assert tasks.list_step_ids(1) == [1, 2, 3, 10]

    def test_skips_non_id_dirs_and_files(self, tmp_storage):
        _seed_run(tmp_storage, 1, 1, 1)
        (tmp_storage / "1" / "scratch").mkdir()
        (tmp_storage / "1" / "notes.txt").write_text("x", encoding="utf-8")
        assert tasks.list_step_ids(1) == [1]

    def test_missing_task_dir_returns_empty(self, tmp_storage):
        assert tasks.list_step_ids(999) == []

    def test_does_not_judge_emptiness(self, tmp_storage):
        """空 step 目录照样列出：「两轨都没段算不算数」是 HLS 域知识，本域不做这个判断。"""
        (tmp_storage / "1" / "5").mkdir(parents=True)
        assert tasks.list_step_ids(1) == [5]


class TestIds:
    def test_sorted_ascending(self, tmp_storage):
        for task_id in (30, 1, 200):
            _seed_run(tmp_storage, task_id, 1, 1)
        assert tasks.list_task_ids() == [1, 30, 200]

    def test_skips_non_id_entries(self, tmp_storage):
        """存储根下不只有数字 task 目录：lab 导出临时根 `.lab_exports/`（clip_builder /
        step_exporter）与送标运行时配置 `lab_runtime_config.json`（services/lab/runtime_config）都寄居
        于此，外加误建的目录——一律跳过，不报错。"""
        _seed_run(tmp_storage, 1, 1, 1)
        (tmp_storage / ".lab_exports").mkdir()
        (tmp_storage / "lab_runtime_config.json").write_text("{}", encoding="utf-8")
        assert tasks.list_task_ids() == [1]

    def test_missing_root_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "nope"))
        assert tasks.list_task_ids() == []
        assert tasks.list_task_ids(order="recent") == []

    def test_recent_order_is_by_latest_run_id(self, tmp_storage):
        _seed_run(tmp_storage, 1, 1, 100)
        _seed_run(tmp_storage, 2, 1, 900)
        assert tasks.list_task_ids(order="recent") == [2, 1]

    def test_recent_order_takes_max_over_steps_and_runs(self, tmp_storage):
        _seed_run(tmp_storage, 1, 1, 500)
        _seed_run(tmp_storage, 1, 2, 950)                 # task 1 的另一个 step 更晚
        _seed_run(tmp_storage, 2, 1, 900)
        _seed_run(tmp_storage, 2, 1, 100)
        assert tasks.list_task_ids(order="recent") == [1, 2]

    def test_recent_order_keeps_task_without_runs_last(self, tmp_storage):
        """没有 run 目录的 task 排序键取 0（排最后），但**仍保留在结果里**
        ——它是否该丢弃由调用方深扫时决定，不是本域的判断。"""
        _seed_run(tmp_storage, 1, 1, 100)
        (tmp_storage / "2" / "1").mkdir(parents=True)          # 有 step、无 run
        assert tasks.list_task_ids(order="recent") == [1, 2]

    def test_recent_tie_breaks_on_larger_task_id(self, tmp_storage):
        for task_id in (1, 2):
            _seed_run(tmp_storage, task_id, 1, 100)
        assert tasks.list_task_ids(order="recent") == [2, 1]

    def test_invalid_order_raises(self, tmp_storage):
        """传错 order 说明调用方对返回顺序有预期，静默按默认走比报错更坏。"""
        with pytest.raises(ValueError, match="Invalid order"):
            tasks.list_task_ids(order="recency")
