"""Small synthetic archives exercise installation without downloads or devices."""

import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
from unittest.mock import Mock
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / ".claude/skills/install/scripts/install_binaries.py"
spec = importlib.util.spec_from_file_location("install_binaries", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def make_archive(path, files, windows):
    if windows:
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
    else:
        with tarfile.open(path, "w:gz") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(content))


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("component", ["ffmpeg", "mediamtx"])
def test_install_platform_archives(tmp_path, monkeypatch, windows, component):
    name = component + (".exe" if windows else "")
    member = f"build/bin/{name}" if component == "ffmpeg" else name
    files = {member: b"new binary"}
    if component == "ffmpeg":
        files["build/bin/companion.dll"] = b"companion"
    archive = tmp_path / "source"
    make_archive(archive, files, windows)
    project = tmp_path / "project"
    target = project / f".{component}"
    target.mkdir(parents=True)
    (target / "old").write_bytes(b"old")
    download = Mock(side_effect=lambda *args, **kwargs: archive.open("rb"))
    monkeypatch.setattr(installer.urllib.request, "urlopen", download)
    monkeypatch.setattr(installer.platform, "system", lambda: "Windows" if windows else "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "AMD64" if windows else "x86_64")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "test version\n", ""))
    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.install_binary(component, "https://source.example", project)
    installed = target / "bin" / name if component == "ffmpeg" else target / name
    assert installed.read_bytes() == b"new binary"
    assert not (target / "old").exists()
    if component == "ffmpeg":
        assert (target / "bin/companion.dll").read_bytes() == b"companion"
    expected_suffix = "win-x64.zip" if windows else "linux-x64.tar.gz"
    if component == "ffmpeg" and not windows:
        expected_suffix = "linux-x64.tar.xz"
    assert download.call_args.args[0].endswith(f"{component}-{expected_suffix}")
    assert run.call_args.kwargs["check"] is True
    assert not list(project.glob(f".{component}-install-*"))
    assert not list(project.glob(f".{component}-previous-*"))


@pytest.mark.parametrize("failure", ["download", "layout", "executable"])
def test_failed_install_keeps_old_binary(tmp_path, monkeypatch, failure):
    archive = tmp_path / "source"
    member = "wrong-file" if failure == "layout" else "mediamtx"
    make_archive(archive, {member: b"new"}, False)
    project = tmp_path / "project"
    target = project / ".mediamtx"
    target.mkdir(parents=True)
    (target / "mediamtx").write_bytes(b"old")
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    download = Mock(side_effect=OSError("offline")) if failure == "download" else Mock(side_effect=lambda *a, **k: archive.open("rb"))
    monkeypatch.setattr(installer.urllib.request, "urlopen", download)
    monkeypatch.setattr(installer.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, "mediamtx")))
    with pytest.raises((OSError, ValueError, subprocess.CalledProcessError)):
        installer.install_binary("mediamtx", "https://source.example", project)
    assert (target / "mediamtx").read_bytes() == b"old"


def test_publish_failure_restores_old_directory(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "binary").write_bytes(b"old")
    staged = tmp_path / "ready"
    staged.mkdir()
    original_rename = Path.rename

    def rename(path, destination):
        if path == staged:
            raise OSError("publish failed")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError, match="publish failed"):
        installer.replace_directory(staged, target, tmp_path / "backup")
    assert (target / "binary").read_bytes() == b"old"


def test_restore_failure_keeps_backup(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "binary").write_bytes(b"old")
    staged = tmp_path / "ready"
    staged.mkdir()
    backup = tmp_path / "backup"
    original_rename = Path.rename

    def rename(path, destination):
        if path in (staged, backup):
            raise OSError("directory locked")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError, match="previous installation preserved"):
        installer.replace_directory(staged, target, backup)
    assert (backup / "binary").read_bytes() == b"old"


@pytest.mark.parametrize("windows", [False, True])
def test_archive_cannot_escape_staging_directory(tmp_path, windows):
    archive = tmp_path / "source"
    make_archive(archive, {"../outside": b"unsafe"}, windows)
    with pytest.raises(ValueError, match="Unsafe archive path"):
        installer.extract_archive(archive, tmp_path / "staging")
    assert not (tmp_path / "outside").exists()
