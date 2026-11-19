"""Install archived FFmpeg/MediaMTX without importing application settings."""

import argparse
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def extract_archive(archive: Path, destination: Path) -> None:
    """Extract regular files only, rejecting paths outside the staging directory."""
    def output_path(name: str) -> Path:
        relative = PurePosixPath(name.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or ":" in name:
            raise ValueError(f"Unsafe archive path: {name}")
        return destination.joinpath(*relative.parts)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                target = output_path(member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Archive link is unsupported: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        return

    with tarfile.open(archive) as package:
        for member in package:
            target = output_path(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError(f"Unsupported archive entry: {member.name}")


def find_payload(extracted: Path, component: str, windows: bool) -> tuple[Path, Path]:
    name = component + (".exe" if windows else "")
    candidates = list(extracted.rglob(name))
    candidates = [path for path in candidates if path.is_file()]
    if len(candidates) != 1:
        raise ValueError(f"Expected one {name}, found {len(candidates)}")
    executable = candidates[0]
    if component == "ffmpeg":
        if executable.parent.name != "bin":
            raise ValueError("FFmpeg archive must contain bin/ffmpeg")
        return executable.parent.parent, executable
    return executable.parent, executable


def replace_directory(staged: Path, target: Path, backup: Path) -> None:
    """Restore the previous directory if publishing the new directory fails."""
    had_target = target.exists()
    if had_target:
        target.rename(backup)
    try:
        staged.rename(target)
    except OSError:
        if had_target:
            try:
                backup.rename(target)
            except OSError as error:
                raise OSError(f"Restore failed; previous installation preserved at {backup}") from error
        raise


def install_binary(component: str, base_url: str, project: Path) -> None:
    system = platform.system()
    if system not in ("Linux", "Windows") or platform.machine().lower() not in ("x86_64", "amd64"):
        raise ValueError("Archived binaries support Linux/Windows x86_64 only")
    windows = system == "Windows"
    suffix = "win-x64.zip" if windows else "linux-x64.tar.gz"
    if component == "ffmpeg" and not windows:
        suffix = "linux-x64.tar.xz"
    url = f"{base_url.rstrip('/')}/vendor/{component}/{component}-{suffix}"
    project.mkdir(parents=True, exist_ok=True)
    target = project / f".{component}"
    with tempfile.TemporaryDirectory(prefix=f".{component}-install-", dir=project) as temporary:
        work = Path(temporary)
        archive = work / "archive"
        print(f"Downloading {url}", flush=True)
        with urllib.request.urlopen(url, timeout=60) as source, archive.open("wb") as output:
            shutil.copyfileobj(source, output)
        extracted = work / "extracted"
        extracted.mkdir()
        extract_archive(archive, extracted)
        payload, executable = find_payload(extracted, component, windows)
        executable.chmod(executable.stat().st_mode | 0o111)
        flag = "-version" if component == "ffmpeg" else "--version"
        result = subprocess.run(
            [str(executable), flag], check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        print(result.stdout.splitlines()[0] if result.stdout else f"{component}: executable OK")
        staged = work / "ready"
        if component == "ffmpeg":
            shutil.copytree(payload, staged)
        else:
            staged.mkdir()
            shutil.copy2(executable, staged / executable.name)
            for license_file in payload.glob("LICENSE*"):
                if license_file.is_file():
                    shutil.copy2(license_file, staged / license_file.name)
        backup = project / f".{component}-previous-{uuid.uuid4().hex}"
        replace_directory(staged, target, backup)
        if backup.exists():
            shutil.rmtree(backup)
    print(f"Installed {component} in {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("ffmpeg", "mediamtx", "all"))
    parser.add_argument("--base-url", required=True, help="Binary distribution server URL")
    args = parser.parse_args()
    components = ("ffmpeg", "mediamtx") if args.component == "all" else (args.component,)
    try:
        for component in components:
            install_binary(component, args.base_url, PROJECT_ROOT)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        parser.exit(1, f"Installation failed: {error}\n")


if __name__ == "__main__":
    main()
