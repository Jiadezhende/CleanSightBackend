"""离线分析子进程侧 —— 单次 worker 运行的运行环境与 spawn 入口。

    worker_main(send_conn, task, step, run, threads)   spawn target：钉环境 → 跑一次 run → 回传结果
    execute(task, step, run, threads) -> dict          CLI `run` 与子进程共用的一次执行（异常转 error 结果）
    configure_process(threads)                         钉卡 + 限 torch 线程

**模块顶层只 import stdlib**：multiprocessing spawn 会在 target 执行前先 import 本模块，而
`CUDA_VISIBLE_DEVICES` 必须先于任何 torch import 就位（与在线推理子进程同一导入序契约，见
online/detection/stage_worker.py）。故 worker / torch / app.settings 一律在函数体内 import。

子进程的终止（取消 / 超时 / 停机）统一由父进程 kill 驱动：本模块只忽略 SIGINT，不自行收摊。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[4]

# Windows BELOW_NORMAL_PRIORITY_CLASS（winbase.h）；POSIX 用 os.nice 的增量。
_WINDOWS_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_NICE_INCREMENT = 15


def configure_process(num_threads: int) -> None:
    """钉卡 + 限 torch 线程。**须在任何 torch import 之前调用。**"""
    _pin_cuda_device()
    try:
        import torch  # 此处才首次 import torch，CUDA_VISIBLE_DEVICES 已钉好
        torch.set_num_threads(max(1, num_threads))
    except Exception:  # torch 未安装 / 占位策略不需要 torch —— 不阻断
        pass


def _pin_cuda_device() -> None:
    """可见卡钉成在线推理子进程那一张（`settings.cuda_device`，""=CPU）。"""
    from app.settings import settings

    os.environ["CUDA_VISIBLE_DEVICES"] = settings.cuda_device


def execute(task_id: int, step_id: int, run_id: Optional[int], threads: int) -> Dict[str, Any]:
    """在当前进程里跑一次离线 run，异常转成 `status="error"` 的结果。

    结果形状即 CLI 末行 JSON 与作业服务结果通道的 payload，CLI `run` 与 spawn 子进程共用。
    """
    configure_process(threads)
    # worker / 策略 import 放在隔离之后：策略模块的 torch import 此时才发生
    from .worker import OfflineRunSpec, OfflineWorker

    try:
        result = OfflineWorker().run(
            OfflineRunSpec(task_id=task_id, step_id=step_id, run_id=run_id)
        )
    except Exception as e:  # 配置 / 输入 / 策略 / 写失败
        logger.error("运行失败 task=%s step=%s: %s", task_id, step_id, e, exc_info=True)
        return _error_payload(str(e))
    return {
        "status": result.status,
        "producer": result.producer,
        "segment_count": result.segment_count,
        "alarm_count": result.alarm_count,
        "message": result.message,
    }


def _error_payload(message: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "producer": None,
        "segment_count": 0,
        "alarm_count": None,
        "message": message,
    }


def worker_main(
    send_conn, task_id: int, step_id: int, run_id: Optional[int], threads: int,
) -> None:
    """spawn target：初始化子进程环境 → 跑一次 run → 结果发回父进程。"""
    _configure_child_process()
    payload = execute(task_id, step_id, run_id, threads)
    try:
        send_conn.send(payload)
    except Exception as e:  # 父进程已退出 / 通道已关：结果送不到，留日志即可
        logger.warning("结果回传失败（父进程可能已退出）: %s", e)
    finally:
        try:
            send_conn.close()
        except Exception:  # pragma: no cover - 关闭失败不改变退出语义
            pass
    sys.exit(1 if payload["status"] == "error" else 0)


def _configure_child_process() -> None:
    """spawn 子进程初始化：信号、日志、cwd 契约与优先级。"""
    # Ctrl-C 会广播给整个进程组；忽略它，关停统一由父进程 kill 驱动（同在线推理子进程）。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [offline-worker] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # 保留原 CLI 子进程的 cwd 契约：模型/骨干是 `./app/data` 这类相对配置，按仓库根解析。
    os.chdir(_REPO_ROOT)
    _lower_priority()
    logger.info("子进程启动 pid=%s", os.getpid())


def _lower_priority() -> None:
    """平台相应的低优先级；失败只告警（降优先级不是正确性条件）。"""
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(
                kernel32.GetCurrentProcess(), _WINDOWS_BELOW_NORMAL_PRIORITY_CLASS,
            )
        else:
            os.nice(_NICE_INCREMENT)
    except Exception as e:
        logger.warning("降低进程优先级失败: %s", e)
