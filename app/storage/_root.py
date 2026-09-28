"""
存储根与目录定位 —— 包内各域文件的共同底座，**包内私有**。

只做定位，外加域目录的「只建一级」。枚举归 `tasks.py` / `runs.py`，删除归 `_fs`。各域文件
向本模块要目录，自己拼域内的文件名。

    DOMAINS                         run 下的域子目录白名单（封闭集合，唯一真源）
    path(task, step)                逐级定位 {root}/{task}/{step}，不建目录
    run_ids(task, step)             该 step 下的 run 目录 id，升序
    run_path(run, domain)           定位 {root}/{task}/{step}/{run_id}/{domain}，不建目录
    domain_dir(run, domain, create=) 域文件的统一入口；create 只建域这一级
    dir_name_to_int(name)           目录名 → id；非数字目录名 → None

## 各域文件的用法：先声明一个绑死自己域名的 domain_dir

```python
from app.storage import _root

_DOMAIN = "hls"


def domain_dir(run: RunIdentity, *, create: bool = False) -> Path:
    return _root.domain_dir(run, _DOMAIN, create=create)
```

域名因此在一个域文件里只出现一次。⚠ **别把 helper 命名成 `_root`**：同名函数会把模块名遮掉。

依赖上界：stdlib + `app.types.run`。`settings` 只在函数体内 import。规范见
`docs/kb/DESIGN_STORAGE_LAYER.md` §3。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from app.types.run import RunIdentity

# run 目录下的产物域子目录 —— 封闭集合，唯一真源。域名属于「布局」归本包，产物文件名
# 属于「内容」归各域自己。新增一个域要改这里，这是有意的。
DOMAINS: Tuple[str, ...] = ("hls", "inference")

# (settings.storage_dir 原始值, 解析后的绝对路径)
_root_cache: Optional[Tuple[str, Path]] = None


def _storage_root() -> Path:
    """存储根目录（绝对路径）。值与解析规则归 `app.settings`，本模块只解析一次并缓存。

    缓存 key 取 `settings.storage_dir` **原始字符串**，`tmp_storage` fixture 的
    `monkeypatch.setattr(settings, "storage_dir", ...)` 才能让它自动重算。**不能改写成
    模块级常量**——那会让 patch 失效，表现是测试去写真实 `database/`。
    """
    global _root_cache
    from app.settings import settings

    raw = settings.storage_dir
    if _root_cache is None or _root_cache[0] != raw:
        _root_cache = (raw, settings.storage_base_dir)
    return _root_cache[1]


def path(task_id: Optional[int] = None, step_id: Optional[int] = None) -> Path:
    """逐级定位：`path()` → `{root}`，`path(1)` → `{root}/1`，`path(1, 2)` → `{root}/1/2`。不建目录。

    Raises:
        ValueError: 给了 step_id 却省了 task_id。
    """
    if task_id is None and step_id is not None:
        raise ValueError("task_id is required when step_id is given")
    located = _storage_root()
    if task_id is not None:
        located = located / str(task_id)
        if step_id is not None:
            located = located / str(step_id)
    return located


def dir_name_to_int(name: str) -> Optional[int]:
    """目录名转 int；非数字（临时目录、误建的目录）返回 None。

    task / step / run 目录一律以十进制 id 命名，故这就是「它是不是一个 task/step/run 目录」的判据。
    """
    try:
        return int(name)
    except (TypeError, ValueError):
        return None


def run_ids(task_id: int, step_id: int) -> List[int]:
    """该 step 下的 run 目录 id，升序。step 不在 / 不可读返回 `[]`；非数字名与文件跳过。"""
    try:
        entries = list(path(task_id, step_id).iterdir())
    except OSError:
        return []
    ids = [
        run_id
        for entry in entries
        if (run_id := dir_name_to_int(entry.name)) is not None and entry.is_dir()
    ]
    ids.sort()
    return ids


def run_path(run: RunIdentity, domain: Optional[str] = None) -> Path:
    """`{root}/{task}/{step}/{run_id}[/{domain}]`。只定位，不建目录。

    Raises:
        ValueError: domain 不在 `DOMAINS` 白名单里（笔误会静默造出多余的域目录）。
    """
    located = path(run.task_id, run.step_id) / str(run.run_id)
    if domain is None:
        return located
    if domain not in DOMAINS:
        raise ValueError(f"Unknown domain: {domain!r}, expected one of {DOMAINS}")
    return located / domain


def domain_dir(run: RunIdentity, domain: str, *, create: bool = False) -> Path:
    """该 run 下的域目录。

    `create=True` 只建域这一级（`_fs.ensure_dir`）：**写者不建 run 目录**，run 被回收后的迟到
    写入在这里 `FileNotFoundError`，不会重建出僵尸目录。

    Raises:
        TypeError: run 不是 `RunIdentity`。
        ValueError: domain 不在白名单里（早于建目录）。
    """
    if not isinstance(run, RunIdentity):
        raise TypeError(f"须是 RunIdentity，收到 {type(run).__name__}")
    located = run_path(run, domain)
    if create:
        from . import _fs

        _fs.ensure_dir(located)
    return located

