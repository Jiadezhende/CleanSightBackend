"""Check installed dependencies without loading application configuration."""

import argparse
from importlib import metadata
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def check_opencv() -> None:
    import cv2

    installed = {dist.metadata["Name"].lower().replace("_", "-") for dist in metadata.distributions()}
    if "opencv-python" in installed or "opencv-contrib-python" in installed:
        raise RuntimeError("GUI OpenCV package is installed; follow the skill's headless repair step")
    gui = [line for line in cv2.getBuildInformation().splitlines() if line.strip().startswith("GUI:")]
    if not gui or gui[0].split(":", 1)[1].strip() != "NONE":
        raise RuntimeError("OpenCV must use a headless build")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencv-only", action="store_true", help="Check whether OpenCV repair is needed")
    args = parser.parse_args()
    try:
        if not (3, 10) <= sys.version_info[:2] <= (3, 12):
            raise RuntimeError("Python 3.10–3.12 is required")
        if args.opencv_only:
            check_opencv()
            print("OpenCV headless OK")
            return
        config = PROJECT_ROOT / ".ultralytics"
        config.mkdir(exist_ok=True)
        os.environ["YOLO_CONFIG_DIR"] = str(config)
        import torch
        import torchvision
        import numpy
        from ultralytics import YOLO

        check_opencv()
        print(f"Imports OK (Python {sys.version.split()[0]})")
        extension = ".exe" if os.name == "nt" else ""
        for executable, flag in (
            (PROJECT_ROOT / ".ffmpeg" / "bin" / f"ffmpeg{extension}", "-version"),
            (PROJECT_ROOT / ".mediamtx" / f"mediamtx{extension}", "--version"),
        ):
            subprocess.run([str(executable), flag], check=True, capture_output=True, timeout=30)
            print(f"{executable.name}: executable OK")
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Environment check failed: {error}\n")


if __name__ == "__main__":
    main()
