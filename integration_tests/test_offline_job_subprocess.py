"""
离线作业服务 → 真 spawn 子进程 端到端测试

不需要后端服务、RTSP、数据库、GPU 或模型权重：`OfflineJobService` 用默认 spawner（服务内的
`_spawn`）真起一个 multiprocessing 子进程，子进程直接执行 `OfflineWorker.run`，
验证 spawn / 单向结果通道 / 运行环境继承（存储根、配置）这条管线。

做法：服务侧注入的配置放行 step 987，子进程侧读真实 YAML、987 未配置 → Worker 抛错 →
子进程回传 error 结果 → job 记 failed，message 来自子进程读到的真实配置。不跑模型、不碰权重。
存储根指到临时目录，经环境变量 `CLEANSIGHT_STORAGE_DIR` 传给子进程（spawn 子进程继承 os.environ），结束即删。

用法:
    python integration_tests/test_offline_job_subprocess.py [--timeout 120]

参数:
    --timeout <float>  等子进程结束的上限秒数（默认 120；子进程冷启动 import 较慢）
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.inference.config import InferenceConfig
from app.services.inference.offline.service import OfflineJobService
from app.settings import settings
from app.storage import inference as inference_store

TASK_ID, STEP_ID = 1, 987  # 987 只在服务侧注入的配置里有，真实 YAML 里没有

# 提交校验只看「step 在配置里且 offline 非空」，class 不会被 import（子进程才实例化）。
_CFG = InferenceConfig({"stages": {str(STEP_ID): {"offline": {"class": "unused.Segmenter"}}}})


def run(timeout: float) -> bool:
    from factories import make_detector_output, make_frame_detection, make_run

    run = make_run(TASK_ID, STEP_ID)
    inference_store.append_detections(run, [
        make_frame_detection(ts=1.0, by_source={"x": make_detector_output(n=1, ts=1.0)})
    ])
    svc = OfflineJobService(config=_CFG, poll_s=0.05)
    svc.start()
    try:
        svc.submit(run)
        deadline = time.monotonic() + timeout
        while svc.get(run).status in ("queued", "running"):
            if time.monotonic() >= deadline:
                print(f"FAIL 子进程 {timeout:.0f}s 内未结束")
                return False
            time.sleep(0.05)
        job = svc.get(run)
    finally:
        svc.stop(timeout=5.0)

    ok = job.status == "failed" and str(STEP_ID) in job.message and "未在推理配置中定义" in job.message
    print(f"{'PASS' if ok else 'FAIL'} status={job.status} message={job.message!r}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="离线作业服务 → 真 spawn 子进程 端到端测试")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="等子进程结束的上限秒数（默认 120）")
    args = parser.parse_args()

    # factories 在 tests/ 下，与 tests 共用一份构造逻辑
    sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))
    with tempfile.TemporaryDirectory(prefix="cleansight_offline_") as tmp:
        os.environ["CLEANSIGHT_STORAGE_DIR"] = tmp  # 子进程侧
        settings.storage_dir = tmp                  # 本进程侧（写检测结果）
        ok = run(args.timeout)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
