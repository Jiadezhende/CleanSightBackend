"""
存储根与目录定位 —— 包内各域文件的共同底座。

**包内私有**：它交出去就等于交出「根 + 两级 id + 域」的拼装能力，settings 那道门禁就白设
了（拦属性访问拦不住 `from ..._root import path`）。前导下划线与
`tests/test_import_hygiene.py` 的 root 访问面用例两道一起锁。

**只做两件事：定位，以及按需确保目录存在。** 各域文件（`tasks` / `hls` / `feature` /
`lab`）向本模块要目录，自己拼域内的文件名。

- `mkdir` 在这里，是因为「定位」与「确保能往这写」是同一件事的两半，每个写者都要做一遍
  ——放各域文件里就是三份重复的 `mkdir(parents=True, exist_ok=True)`。默认 `create=False`，
  不主动碰盘。
- **枚举与删除不在这里**：`iterdir` / `stat` / `rmtree` 是 `tasks` 域的能力，混进本模块，
  各域文件 import 它时会顺带吃一层与自己无关的 IO 语义。

## 各域文件的用法：先声明一个绑死自己域名的私有 root

每个域文件（`hls` / `feature` / `lab`）**开头先声明一个私有 root**，域内所有路径函数都经
它，不再直接调本模块：

```python
# hls.py
from app.storage import _root

_DOMAIN = "hls"


# 本域在该 step 下的根目录 —— 域内所有路径函数都经它
def _domain_root(task_id: int, step_id: int, *, create: bool = False) -> Path:
    return _root.path(task_id, step_id, _DOMAIN, create=create)


def segment_path(task_id, step_id, track, ts_us) -> Path:
    return _domain_root(task_id, step_id) / _segment_name(track, ts_us)
```

两个收益：**域名在一个域文件里只出现一次**（写错一眼可见，且白名单会当场拦下），以及域内
每个路径函数少写一个参数。

> ⚠ **别把它命名成 `_root`**：域文件顶部有 `from app.storage import _root`，同名
> 函数会把模块名遮掉，`_root.path` 立刻 `AttributeError`。用 `_domain_root` 或 `_dir`。

重域拆成子包时（`hls/`），这个 helper 要让同包的兄弟模块用得上（删整个域目录、枚举域内
文件），**去掉前导下划线叫 `domain_dir`**——包内公开、仍不出包（facade 不 re-export 它）。

依赖上界：stdlib only。`settings` 是 L3（读环境有副作用），只在函数体内 import（规范 §2）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

# step 目录下的产物域子目录 —— **封闭集合，唯一真源**。
#
# 域名属于「布局」，归本包；产物文件名属于「内容」，归各域自己（hls 五类在 hls.py，
# features.jsonl / facts.jsonl 在 inference/）。这条分界决定了白名单该放在这里。
#
# **为什么必须白名单校验**：域名打错（"feature" / "hls_" / "HLS"）会静默造出一个新的
# 子目录，产物落进去谁也不知道——写侧不报错，读侧只是"查不到"，而 purge_step 照样把它
# 删掉，于是连残留证据都不留。这正是「会静默失败的知识必须收进包」的判据。
# 校验顺带挡住路径逃逸（"../.."），虽然域名不是外部输入，但零成本。
#
# 新增一个域要改这里 —— 这是有意的：往 step 目录里塞一个新子目录该是一次显式决策。
DOMAINS: Tuple[str, ...] = ("hls", "features", "lab")

# (settings.storage_dir 原始值, 解析后的绝对路径)。见 _storage_root() 的记忆化说明。
_root_cache: Optional[Tuple[str, Path]] = None


def _storage_root() -> Path:
    """存储根目录（绝对路径）。

    值与解析规则都归 `app.settings`（`storage_base_dir`，相对路径以项目根为基，与
    `config_dir` 同款），本模块只负责**解析一次并缓存**。

    **为什么要缓存**：`settings.storage_base_dir` 每次访问都跑 `.resolve()`，那是文件
    系统 syscall。本包的定位函数会被每段落盘、每次请求各调若干次，没必要每次都问盘。

    **为什么 key 取 `settings.storage_dir` 原始字符串、而不是无条件缓存**：
    `tests/conftest.py` 的 `tmp_storage` fixture 靠 `monkeypatch.setattr(settings,
    "storage_dir", ...)` 把读写两侧一起指到临时目录。以原始值作 key，patch 后 key 变、
    自动重算，测试无需知道本模块有缓存。

    **不能改写成模块级常量**（`_ROOT = settings.storage_base_dir`）：那在 import 期就
    定死，上面那条 patch 会完全失效，失效的表现是**测试去写真实 `database/`**。
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

    一个入口而不是 `root()`/`task_dir()`/`step_dir()`/`domain_dir()` 四个：它们是同一条
    路径的四个前缀，用参数表达比用四个名字表达更直白，也不会有人拼出第五种组合。

    Args:
        create: True 时 `mkdir(parents=True, exist_ok=True)` 到返回的那一级。
            **写任何产物前用它。** 校验一律早于 mkdir——否则非法 domain 的目录已经落盘了
            才报错。建目录失败时 `OSError` 原样抛出，由调用方决定包成什么
            （`hls_strategy` 现在包成 `PersistenceError(retryable=True)`）。

    Returns:
        绝对路径。`create=False` 时不保证任何一级存在，也不创建任何目录——读一个不存在的
        step 不该在盘上留空目录（空目录会被 `tasks.ids()` 列出却没有任何内容）。

    Raises:
        ValueError: 跳级（给了深层却省了浅层），或 domain 不在 `DOMAINS` 白名单里。

    > **省掉 `domain` 是合法的，但只有 `tasks.py` 该这么用**（枚举 step、删整个 step）。
    > 各域文件一律传满三级——见下方「各域文件的用法」。
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
    """目录名转 int；非数字（如临时目录、误建的目录）返回 None。

    存储根下 task 目录、task 目录下 step 目录一律以十进制 id 命名，故「目录名能不能转
    int」就是「它是不是一个 task/step 目录」的判据。非 id 目录由此天然被枚举跳过。
    """
    try:
        return int(name)
    except (TypeError, ValueError):
        return None
