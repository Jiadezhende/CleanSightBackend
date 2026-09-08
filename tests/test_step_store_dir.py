"""
`step_store.store` 单元测试 —— `(task, step)` 目录契约，所有域共享的那一层。

覆盖：
- steps / tasks：落盘枚举（出 id 对，不出句柄），含 recent_first 粗排
- file_path / open_file：域自己拿名字来，写模式建目录、读模式不留痕
- find_file：外部来的文件名 + path traversal 防御
- 活动标记：写入口刷、读入口与 scratch_path 不刷（TTL 唯一判据）
- scratch_path：前导点命名与落点

HLS 那一摊（段 / 在途过滤 / vod_playlist / 写路径）在 test_step_store_hls.py。

存储根一律经 `tmp_storage` fixture 指到临时目录（monkeypatch `settings.storage_dir`）——
本包不收 base_dir 参数，根是单一真源。
"""

import os
from pathlib import Path

import pytest

from app.services.step_store import store


def _make_step_dir(base: Path, task_id: int, step_id: int) -> Path:
    d = base / str(task_id) / str(step_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSteps:
    """steps()：**出全部两层数字目录，不筛内容** —— 筛内容是各域的事。"""

    def test_returns_empty_when_task_dir_missing(self, tmp_storage):
        assert store.steps(999) == []

    def test_lists_id_pairs_sorted(self, tmp_storage):
        _make_step_dir(tmp_storage, 7, 2)
        _make_step_dir(tmp_storage, 7, 1)
        assert store.steps(7) == [(7, 1), (7, 2)]

    def test_sees_dirs_without_any_segment(self, tmp_storage):
        """**TTL 的命门**：只有 features.jsonl 没有 HLS 段的目录正是此前泄漏的那一类。

        `hls.list_steps()` 按契约丢弃它（清单不该让前端点开黑屏），本函数必须看见。
        """
        (_make_step_dir(tmp_storage, 7, 1) / "features.jsonl").write_text("{}")
        _make_step_dir(tmp_storage, 7, 2)
        assert store.steps(7) == [(7, 1), (7, 2)]

    def test_skips_non_numeric_step_dirs(self, tmp_storage):
        (tmp_storage / "7" / "scratch").mkdir(parents=True)
        _make_step_dir(tmp_storage, 7, 1)
        assert store.steps(7) == [(7, 1)]

    def test_skips_files_at_task_level(self, tmp_storage):
        (tmp_storage / "lab_runtime_config.json").write_text("{}")
        assert store.steps() == []

    def test_global_enumeration_spans_tasks(self, tmp_storage):
        _make_step_dir(tmp_storage, 7, 1)
        _make_step_dir(tmp_storage, 9, 3)
        assert store.steps() == [(7, 1), (9, 3)]

    def test_missing_base_dir(self, tmp_path, monkeypatch):
        """存储根整个不存在（首次启动、盘没挂上）：返回空而不是抛。"""
        from app.settings import settings

        monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "nope"))
        assert store.steps() == []


class TestTasks:
    def test_returns_empty_when_base_dir_missing(self, tmp_storage, monkeypatch):
        from app.settings import settings

        monkeypatch.setattr(settings, "storage_dir", str(tmp_storage / "nope"))
        assert store.tasks() == []

    def test_skips_non_numeric_dirs_and_sorts(self, tmp_storage):
        for task_id in (30, 10, 20):
            _make_step_dir(tmp_storage, task_id, 1)
        (tmp_storage / ".lab_exports").mkdir()
        (tmp_storage / "README.md").write_text("x")

        assert store.tasks() == [10, 20, 30]

    def test_recent_first_orders_by_newest_step_dir_mtime(self, tmp_storage):
        for task_id, mtime in ((1, 1_000), (2, 3_000), (3, 2_000)):
            d = _make_step_dir(tmp_storage, task_id, 1)
            os.utime(d, (mtime, mtime))  # 确定的 mtime，不靠写入顺序

        assert store.tasks(recent_first=True) == [2, 3, 1]

    def test_recent_first_uses_max_step_mtime_within_a_task(self, tmp_storage):
        os.utime(_make_step_dir(tmp_storage, 1, 1), (1_000, 1_000))
        os.utime(_make_step_dir(tmp_storage, 1, 2), (9_000, 9_000))
        os.utime(_make_step_dir(tmp_storage, 2, 1), (5_000, 5_000))

        # task 1 取 max(1000, 9000)=9000 → 排在 task 2 前面
        assert store.tasks(recent_first=True) == [1, 2]

    def test_task_without_step_dirs_sorts_last_but_is_kept(self, tmp_storage):
        os.utime(_make_step_dir(tmp_storage, 1, 1), (1_000, 1_000))
        (tmp_storage / "2").mkdir()  # 空 task 目录：排序键 0，仍保留由调用方深扫丢弃

        assert store.tasks(recent_first=True) == [1, 2]


class TestFileEntries:
    """域自己拿着文件名来，本层只负责「这个 step 目录里的一个文件」。"""

    def test_file_path_lands_in_step_dir(self, tmp_storage):
        p = store.file_path(1, 1, "features.jsonl")
        assert p.name == "features.jsonl"
        assert p.parent == tmp_storage / "1" / "1"

    def test_file_path_creates_step_dir(self, tmp_storage):
        store.file_path(1, 1, "features.jsonl")
        assert (tmp_storage / "1" / "1").is_dir()

    def test_open_roundtrip(self, tmp_storage):
        with store.open_file(1, 1, "features.jsonl", "a") as f:
            f.write('{"ts": 1}\n')
        with store.open_file(1, 1, "features.jsonl") as f:
            assert f.read() == '{"ts": 1}\n'

    def test_open_read_missing_raises(self, tmp_storage):
        with pytest.raises(FileNotFoundError):
            store.open_file(1, 1, "facts.jsonl")


class TestFindFile:
    """按外部给的文件名取文件 —— path traversal 防御在包内，不在协议层。"""

    def test_returns_path_for_existing_file(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        (d / "raw_segment_100.mp4").write_bytes(b"")
        assert store.find_file(1, 1, "raw_segment_100.mp4") == d / "raw_segment_100.mp4"

    def test_none_for_missing_file(self, tmp_storage):
        _make_step_dir(tmp_storage, 1, 1)
        assert store.find_file(1, 1, "nope.mp4") is None

    @pytest.mark.parametrize(
        "evil", ["../../etc/passwd", "..\\..\\secret", "sub/dir.mp4", "..", "."]
    )
    def test_rejects_traversal(self, tmp_storage, evil):
        _make_step_dir(tmp_storage, 1, 1)
        assert store.find_file(1, 1, evil) is None

    def test_does_not_create_dir_or_stamp(self, tmp_storage):
        assert store.find_file(1, 1, "nope.mp4") is None
        assert not (tmp_storage / "1" / "1").exists()


class TestActivityMarker:
    """TTL 的唯一判据：写入口顺带刷新它，读入口一律不碰。"""

    def test_write_entries_stamp_activity(self, tmp_storage):
        """漏刷的表现是该写者独占的 step 静默过期被回收——静默错误，必须锁住。"""
        writes = [
            lambda t, s: store.file_path(t, s, "features.jsonl"),
            lambda t, s: store.open_file(t, s, "facts.jsonl", "w").close(),
            lambda t, s: store.open_file(t, s, "x.bin", "wb").close(),
        ]
        for i, write in enumerate(writes):
            assert store.last_activity_at(2, i) is None
            write(2, i)
            assert store.last_activity_at(2, i) is not None, write

    def test_read_open_does_not_stamp_or_create_dir(self, tmp_storage):
        """读一个不存在的 step 不该在盘上留痕，也不该让它显得还活着。"""
        with pytest.raises(FileNotFoundError):
            store.open_file(1, 1, "features.jsonl", "r")
        assert not (tmp_storage / "1" / "1").exists()
        assert store.last_activity_at(1, 1) is None

    def test_scratch_path_does_not_stamp(self, tmp_storage):
        """临时文件是读侧导出/打点的中间产物，不代表这个 step 还在产出。"""
        store.scratch_path(1, 1, "export")
        assert store.last_activity_at(1, 1) is None

    def test_none_for_missing_dir(self, tmp_storage):
        assert store.last_activity_at(9, 9) is None


class TestScratchPath:
    def test_leading_dot_keeps_temp_files_out_of_segment_scan(self, tmp_storage):
        """前导点让半截的临时 mp4 落在段名正则之外，不被 `hls.segments()` 当成真段。"""
        p = store.scratch_path(1, 1, "clip")
        assert p.name.startswith(".clip_")
        assert p.suffix == ".m3u8"
        assert p.parent == tmp_storage / "1" / "1"

    def test_suffix_is_overridable(self, tmp_storage):
        p = store.scratch_path(1, 1, "facts", suffix=".jsonl")
        assert p.name.startswith(".facts_") and p.suffix == ".jsonl"

    def test_unique_per_call(self, tmp_storage):
        assert store.scratch_path(1, 1, "clip") != store.scratch_path(1, 1, "clip")


class TestBaseDirResolution:
    """存储根目录由 settings.storage_base_dir 单一真源解析，与进程 cwd 无关。

    「读写两侧同源」是**结构性保证**——除 settings 与本包外没人能读到那个属性，也没人能拿到
    `_storage_root`（两者由 `test_import_hygiene.test_storage_root_is_private_to_step_store`
    一并锁死）。
    """

    def test_relative_base_dir_resolves_to_project_root_regardless_of_cwd(
        self, tmp_path, monkeypatch
    ):
        from app.settings import settings
        from app.services.step_store.store import _step_dir, _storage_root

        monkeypatch.setattr(settings, "storage_dir", "./database")
        write_path = settings.storage_base_dir
        assert write_path.is_absolute()
        assert write_path.name == "database"

        # 切到完全无关的 cwd，解析的绝对路径必须保持不变（以项目根为基，非 cwd）
        monkeypatch.chdir(tmp_path)
        assert settings.storage_base_dir == write_path
        assert tmp_path not in write_path.parents

        # 本包的根解析与 settings 同值，且目录 = 根 + 两级 id
        assert _storage_root() == write_path
        assert _step_dir(1, 2) == write_path / "1" / "2"

    def test_absolute_base_dir_is_returned_as_is(self, tmp_path, monkeypatch):
        from app.settings import settings

        abs_dir = tmp_path / "custom" / "store"
        monkeypatch.setattr(settings, "storage_dir", str(abs_dir))
        assert settings.storage_base_dir == abs_dir.resolve()
