"""段的逐帧时间戳 sidecar：`raw_segment_{ts_us}.idx`。

**二进制布局：float64 裸数组，每帧一条，无头无尾**（`np.ndarray.tofile` / `np.fromfile` 的
原生字节序）。没有 magic、没有长度字段——条数由文件大小 ÷ 8 得出。

**用途是离线反查**：段内第 k 帧的墙钟 ts 无法从 mp4 里问出来（媒体轴是压紧的），而离线要按
墙钟区间取帧。**段内帧号与本数组下标严格 1:1。** 只有 raw 轨产出它（processed 是渲染结果、
离线不消费），这条不对称是有意的。

依赖上界：numpy（本模块的货币就是 float64 数组，没法推迟到函数体内）+ stdlib。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

import numpy as np

_DTYPE = np.float64

# tmp 后缀。与目标**同目录**，`os.replace` 才是原子换名；它也匹配不上段正则。
_TMP_SUFFIX = ".tmp"


def write(path: Path, timestamps: Sequence[float]) -> None:
    """原子写入时间戳数组（tmp + `os.replace`），读侧不会看到半截文件。

    Raises:
        OSError: 写入或换名失败。是否吞掉由调用方定。

    **位级保真是契约**（`FrameTracker` 拿 sidecar 值与内存帧 ts 做相等比较），故走
    `np.asarray(..., dtype=float64)` 而不是逐个 `float()`。
    """
    tmp = path.with_suffix(_TMP_SUFFIX)
    array = np.asarray(list(timestamps), dtype=_DTYPE)
    try:
        tmp.unlink(missing_ok=True)
        with open(tmp, "wb") as f:
            array.tofile(f)
        os.replace(tmp, path)
    except OSError:
        # 清残留 tmp 本身也可能失败（同一个盘的同一个故障），不能让它盖掉原异常
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read(path: Path) -> np.ndarray:
    """回读时间戳数组（`write` 的逆运算）。

    文件不存在返回**空数组**而不是抛——读侧据此跳过该段、不打断整条迭代。
    """
    if not path.exists():
        return np.empty(0, dtype=_DTYPE)
    return np.fromfile(path, dtype=_DTYPE)
