"""process_group.py - 子进程组收尸：terminate → join → kill → join，并拆掉它的队列。

    kill_process_group([proc], queues=(req_q, resp_q), break_reader_of=req_q,
                       logger=logger, label="RemoteInferProxy")

**只 `close()` 救不了阻塞中的 feeder**：它既不关本进程持有的读端（写端拿不到 EPIPE），
也不撤 atexit 里那个无超时的 `thread.join`。队列三步的推导与实测见
`docs/update/20260902_INFER_PROXY_EXIT_HANG.md`。本模块只做父进程侧的收尸——不重启、
不转发请求、不清理调用方的在途状态。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

__all__ = ["kill_process_group"]

_JOIN_TIMEOUT = 2.0


def kill_process_group(
    procs: Iterable[Any] = (),
    *,
    queues: Iterable[Any] = (),
    break_reader_of: Any = None,
    logger: Optional[logging.Logger] = None,
    label: str = "",
) -> None:
    """收掉一组子进程并拆掉它们用的队列。任何一步失败都不抛——收尸路径不为异常改变语义。

    顺序固定（换序会重新引入挂死）：
      1. 每个进程 `terminate → join(2s) → kill → join(2s)`：先给一次体面退出，2s 不退再
         SIGKILL（CUDA D 态下可能杀不掉；`join` 超时不抛，继续往下）。
      2. `break_reader_of._reader.close()`：断本进程这侧**没人用**的读端，让卡在
         `send_bytes` 的 feeder 立刻拿到 EPIPE 自退（= CPython `Queue._terminate_broken`）。
      3. 每条队列 `cancel_join_thread()` + `close()`：注销 atexit 的无超时 join，并释放 fd。

    Args:
        procs: 要收的子进程；`None` 项跳过，空序列只拆队列。
        queues: 要拆的 `multiprocessing.Queue`；`None` 项跳过。
        break_reader_of: 读端在本进程内不用的那条队列（通常是请求队列）；`None` 跳过第 2 步。
        logger: 记录收尸失败用；`None` 则静默。
        label: 日志前缀，形如 `RemoteInferProxy`（输出为 `[label] kill child failed: …`）。
    """
    for proc in procs:
        if proc is None:
            continue
        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=_JOIN_TIMEOUT)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=_JOIN_TIMEOUT)
        except Exception as e:  # 已退出 / 句柄已关：收尸不能因此中断
            if logger is not None:
                logger.warning("[%s] kill child failed: %s", label, e)

    if break_reader_of is not None:
        try:
            break_reader_of._reader.close()
        except Exception:  # 非 multiprocessing 队列 / 读端已关
            pass

    for q in queues:
        if q is None:
            continue
        try:
            q.cancel_join_thread()
        except Exception:  # 非 multiprocessing 队列 / 已注销
            pass
        try:
            q.close()
        except Exception:  # 已关 / 无此方法
            pass
