"""离线策略的中间产物缓存 —— `{run}/inference/cache_{name}.npz` 的读写。

    write_offline_cache(run, "hand_embeddings", {"ts": ..., "emb": ...})   # 整体替换（路线 C）
    read_offline_cache(run, "hand_embeddings")                             # {键: ndarray}；没有则 None

本层只管落位与原子替换，不认识内容：键、dtype、缓存是否仍有效都归写它的策略。盘上只许数值与定长字符串
（读写都 `allow_pickle=False`）。随 run 目录一起由 TTL 回收，本域不删除。

依赖上界：numpy（npz 是本模块的货币）+ stdlib。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Dict, Mapping, Optional

import numpy as np

from app.storage.utils import fs as _fs
from app.types.run import RunIdentity

from . import _layout


def write_offline_cache(run: RunIdentity, name: str, arrays: Mapping[str, np.ndarray]) -> None:
    """**整体替换**该 run 名为 `name` 的缓存。

    Raises:
        ValueError: `name` 不合法，或有对象数组（落盘会走 pickle）。
        OSError: 建目录 / 写 tmp / 换名失败（run 已被回收即 `FileNotFoundError`）。失败时旧文件原样保留。
    """
    filename = _layout.cache_name(name)
    arrays = {k: np.asarray(v) for k, v in arrays.items()}
    objects = [k for k, v in arrays.items() if v.dtype.hasobject]
    if objects:
        raise ValueError(f"缓存只许数值与定长字符串，对象数组: {objects}")
    _fs.replace(_layout.domain_dir(run, create=True) / filename, lambda tmp: _write(arrays, tmp))


def _write(arrays: Dict[str, np.ndarray], tmp: Path) -> None:
    # 传文件对象而非路径：`np.savez` 收到不以 .npz 结尾的路径会自作主张补后缀
    with open(tmp, "wb") as f:
        np.savez(f, **arrays)


def read_offline_cache(run: RunIdentity, name: str) -> Optional[Dict[str, np.ndarray]]:
    """回读缓存，各数组与写入时 dtype / 值相同；文件不存在返回 `None`。

    Raises:
        ValueError: `name` 不合法，或文件损坏（含 `allow_pickle=False` 拒读的对象数组）。
        OSError: 读失败。
    """
    path = _layout.domain_dir(run) / _layout.cache_name(name)
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as npz:
            return {k: npz[k] for k in npz.files}
    except FileNotFoundError:
        raise
    except (EOFError, OSError, zipfile.BadZipFile) as e:
        raise ValueError(f"缓存损坏: {path.name}: {e}") from e
