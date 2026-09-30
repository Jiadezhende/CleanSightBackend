"""`app.storage.utils.fs` 盘上原语：整体替换、原子删除、建一级目录。"""

import os

import pytest

from app.storage.utils import fs as _fs


# ---------------------------------------------------------------------------
# replace
# ---------------------------------------------------------------------------


class TestReplace:
    def test_writes_via_dot_tmp_then_replaces(self, tmp_path):
        target = tmp_path / "a.json"
        seen = []

        def write(tmp):
            seen.append(tmp.name)
            tmp.write_text("new", encoding="utf-8")

        _fs.replace(target, write)

        assert seen == [".a.json.tmp"]
        assert target.read_text(encoding="utf-8") == "new"
        assert [p.name for p in tmp_path.iterdir()] == ["a.json"]

    def test_does_not_create_parent(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _fs.replace(tmp_path / "missing" / "a.json", lambda tmp: tmp.write_text("x"))
        assert not (tmp_path / "missing").exists()

    def test_failed_replace_keeps_old_file_and_no_tmp(self, tmp_path, monkeypatch):
        target = tmp_path / "a.json"
        target.write_text("old", encoding="utf-8")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(_fs.os, "replace", boom)
        with pytest.raises(OSError, match="disk full"):
            _fs.replace(target, lambda tmp: tmp.write_text("new", encoding="utf-8"))

        assert target.read_text(encoding="utf-8") == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["a.json"]

    def test_write_fn_error_propagates_and_cleans_tmp(self, tmp_path):
        def write(tmp):
            tmp.write_text("half", encoding="utf-8")
            raise TypeError("not serializable")

        with pytest.raises(TypeError):
            _fs.replace(tmp_path / "a.json", write)
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# remove / purge_trash
# ---------------------------------------------------------------------------


def _tree(root):
    d = root / "1" / "2"
    (d / "hls").mkdir(parents=True)
    (d / "hls" / "seg.mp4").write_bytes(b"x")
    return d


class TestRemove:
    def test_absent(self, tmp_path):
        assert _fs.remove(tmp_path / "nope", root=tmp_path) is _fs.Removed.ABSENT
        assert list(tmp_path.iterdir()) == []   # 不在就不建回收区

    def test_removes_directory_and_leaves_empty_trash(self, tmp_path):
        d = _tree(tmp_path)
        assert _fs.remove(d, root=tmp_path) is _fs.Removed.REMOVED
        assert not d.exists()
        assert list((tmp_path / _fs.TRASH_NAME).iterdir()) == []

    def test_removes_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        assert _fs.remove(f, root=tmp_path) is _fs.Removed.REMOVED
        assert not f.exists()

    def test_rename_failure_leaves_disk_untouched(self, tmp_path, monkeypatch):
        d = _tree(tmp_path)

        def boom(src, dst):
            raise PermissionError("in use")

        monkeypatch.setattr(_fs.os, "rename", boom)
        assert _fs.remove(d, root=tmp_path) is _fs.Removed.FAILED
        assert (d / "hls" / "seg.mp4").read_bytes() == b"x"

    def test_rmtree_failure_still_removed_and_left_in_trash(self, tmp_path, monkeypatch):
        d = _tree(tmp_path)

        def boom(path, *a, **k):
            raise OSError("busy")

        monkeypatch.setattr(_fs.shutil, "rmtree", boom)
        assert _fs.remove(d, root=tmp_path) is _fs.Removed.REMOVED
        assert not d.exists()
        assert len(list((tmp_path / _fs.TRASH_NAME).iterdir())) == 1

    def test_default_root_is_storage_root(self, tmp_storage):
        d = _tree(tmp_storage)
        assert _fs.remove(d) is _fs.Removed.REMOVED
        assert (tmp_storage / _fs.TRASH_NAME).is_dir()


class TestPurgeTrash:
    def test_missing_trash_is_zero(self, tmp_path):
        assert _fs.purge_trash(root=tmp_path) == 0
        assert list(tmp_path.iterdir()) == []

    def test_purges_leftovers(self, tmp_path):
        trash = tmp_path / _fs.TRASH_NAME
        (trash / "a" / "b").mkdir(parents=True)
        (trash / "f").write_text("x")

        assert _fs.purge_trash(root=tmp_path) == 2
        assert list(trash.iterdir()) == []

    def test_failed_entry_stays_for_next_round(self, tmp_path, monkeypatch):
        (tmp_path / _fs.TRASH_NAME / "a").mkdir(parents=True)

        def boom(path, *a, **k):
            raise OSError("busy")

        monkeypatch.setattr(_fs.shutil, "rmtree", boom)
        assert _fs.purge_trash(root=tmp_path) == 0
        assert (tmp_path / _fs.TRASH_NAME / "a").exists()


# ---------------------------------------------------------------------------
# ensure_dir
# ---------------------------------------------------------------------------


class TestEnsureDir:
    def test_creates_one_level_idempotently(self, tmp_path):
        assert _fs.ensure_dir(tmp_path / "hls") == tmp_path / "hls"
        _fs.ensure_dir(tmp_path / "hls")
        assert (tmp_path / "hls").is_dir()

    def test_does_not_create_parents(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _fs.ensure_dir(tmp_path / "run" / "hls")
        assert not os.path.exists(tmp_path / "run")
