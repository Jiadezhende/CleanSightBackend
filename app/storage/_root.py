"""
存储根与目录定位 —— 包内各域文件的共同底座，**包内私有**。

只做两件事：**定位**，以及**按需确保目录存在**（`create=`）。枚举与删除不在这里，归
`tasks.py`。各域文件向本模块要目录，自己拼域内的文件名。

    DOMAINS              step 下的域子目录白名单（封闭集合，唯一真源）
    path(...)            逐级定位 {root}/{task}/{step}/{domain}，可选建目录
    run_path(run, domain) 定位 {root}/{task}/{step}/{run_id}/{domain}，不建目录
    domain_dir(key, domain, create=)  域文件的统一入口：按位置键解析域目录
    legacy_key           迁移期装饰器：把旧的 (task_id, step_id, ...) 调用包成 LegacyStep
    dir_name_to_int(name) 目录名 → id；非数字目录名 → None

## 位置键（迁移期，第 5 期删 `LegacyStep` / `legacy_key`）

域文件的读写口第一个参数是位置键 `RunKey = RunIdentity | LegacyStep`：

    RunIdentity(task, step, run_id)  → {step}/{run_id}/{domain}/   create 只建域这一级，run 目录不在即 OSError
    LegacyStep(task, step)           → {step}/{domain}/            旧布局，create 建到底

旧调用形态 `f(task_id, step_id, ...)` 经 `legacy_key` 包成 `LegacyStep`，与 `f(run, ...)` 同名并存。

## 各域文件的用法：先声明一个绑死自己域名的私有 root

```python
from app.storage import _root

_DOMAIN = "hls"


# 本域在该 step 下的根目录 —— 域内所有路径函数都经它
def _domain_root(task_id: int, step_id: int, *, create: bool = False) -> Path:
    return _root.path(task_id, step_id, _DOMAIN, create=create)
```

域名因此在一个域文件里只出现一次。重域拆成子包时（`hls/`），这个 helper 要让同包兄弟模块
用得上，去掉前导下划线叫 `domain_dir`——包内公开、仍不出包。

> ⚠ **别把它命名成 `_root`**：域文件顶部有 `from app.storage import _root`，同名函数会把
> 模块名遮掉，`_root.path` 立刻 `AttributeError`。

依赖上界：stdlib only。`settings` 只在函数体内 import。规范见
`docs/kb/DESIGN_STORAGE_LAYER.md` §3。
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple, TypeVar, Union

from app.domain.run import RunIdentity

# step 目录下的产物域子目录 —— 封闭集合，唯一真源。域名属于「布局」归本包，产物文件名
# 属于「内容」归各域自己。新增一个域要改这里，这是有意的。
DOMAINS: Tuple[str, ...] = ("hls", "inference", "lab")

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


def path(
    task_id: Optional[int] = None,
    step_id: Optional[int] = None,
    domain: Optional[str] = None,
    *,
    create: bool = False,
) -> Path:
    """逐级定位目录：`{root}/{task_id}/{step_id}/{domain}/`。

    三个位置参数**从左到右逐级下钻**，省到哪一级就返回哪一级：

        path()                        → {root}
        path(1)                       → {root}/1
        path(1, 2)                    → {root}/1/2
        path(1, 2, "hls")             → {root}/1/2/hls
        path(1, 2, "hls", create=True) → 同上，并保证该目录存在

    Args:
        create: True 时 `mkdir(parents=True, exist_ok=True)` 到返回的那一级。**写任何产物
            前用它。** 校验早于 mkdir；建目录失败时 `OSError` 原样抛出。

    Returns:
        绝对路径。`create=False` 时不保证任何一级存在，也不创建任何目录。

    Raises:
        ValueError: 跳级（给了深层却省了浅层），或 domain 不在 `DOMAINS` 白名单里。

    省掉 `domain` 是合法的，但只有 `tasks.py` 该这么用（枚举 step、删整个 step）；各域
    文件一律传满三级。
    """
    if task_id is None and (step_id is not None or domain is not None):
        raise ValueError("task_id is required when step_id or domain is given")
    if step_id is None and domain is not None:
        raise ValueError("step_id is required when domain is given")
    if domain is not None and domain not in DOMAINS:
        raise ValueError(f"Unknown domain: {domain!r}, expected one of {DOMAINS}")

    located = _storage_root()
    if task_id is not None:
        located = located / str(task_id)
        if step_id is not None:
            located = located / str(step_id)
            if domain is not None:
                located = located / domain

    if create:
        located.mkdir(parents=True, exist_ok=True)
    return located


def dir_name_to_int(name: str) -> Optional[int]:
    """目录名转 int；非数字（临时目录、误建的目录）返回 None。

    task / step 目录一律以十进制 id 命名，故这就是「它是不是一个 task/step 目录」的判据。
    """
    try:
        return int(name)
    except (TypeError, ValueError):
        return None


def run_path(run: RunIdentity, domain: Optional[str] = None) -> Path:
    """`{root}/{task}/{step}/{run_id}[/{domain}]`。只定位，不建目录。

    Raises:
        ValueError: domain 不在 `DOMAINS` 白名单里。
    """
    located = path(run.task_id, run.step_id) / str(run.run_id)
    if domain is None:
        return located
    if domain not in DOMAINS:
        raise ValueError(f"Unknown domain: {domain!r}, expected one of {DOMAINS}")
    return located / domain


@dataclass(frozen=True)
class LegacyStep:
    """旧调用形态 `(task_id, step_id)` 的位置键，指向 `{step}/{domain}/`。迁移期专用。"""

    task_id: int
    step_id: int


RunKey = Union[RunIdentity, LegacyStep]


def domain_dir(key: RunKey, domain: str, *, create: bool = False) -> Path:
    """按位置键解析域目录。

    `create=True`：`RunIdentity` 只建域这一级（`_fs.ensure_dir`），run 目录不在即
    `FileNotFoundError`——写者不建 run 目录，回收后的迟到写入在这里失败；`LegacyStep` 同
    `path(..., create=True)` 建到底。
    """
    if isinstance(key, RunIdentity):
        located = run_path(key, domain)
        if create:
            from . import _fs

            _fs.ensure_dir(located)
        return located
    if isinstance(key, LegacyStep):
        return path(key.task_id, key.step_id, domain, create=create)
    raise TypeError(f"位置键须是 RunIdentity 或 LegacyStep，收到 {type(key).__name__}")


_F = TypeVar("_F", bound=Callable)


def legacy_key(fn: _F) -> _F:
    """让 `fn(key, ...)` 同时接受旧形态 `fn(task_id, step_id, ...)`（包成 `LegacyStep`）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if args and not isinstance(args[0], (RunIdentity, LegacyStep)):
            task_id, step_id, *rest = args
            args = (LegacyStep(task_id, step_id), *rest)
        return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
