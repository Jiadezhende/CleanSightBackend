"""`(task_id, step_id)` step 目录的契约 —— **所有域共享的那一层**（包契约见 `__init__.py`）。

本模块只回答与「哪个域的东西」无关的问题：这个 step 的目录里最后何时有人写过、怎么在里面开
一个文件、怎么把整个目录删掉、盘上有哪些 step。**它不知道任何一类产物的名字**——名字由各域
自己持有（HLS 那五类在 `hls.py`，`features.jsonl` 等在 `inference/`），经 `file_path` /
`open_file` 把名字递进来。

    from app.services.step_store import store as step_store
    step_store.open_file(task_id, step_id, "features.jsonl", "a")

**不出句柄**：全部是收 `(task_id, step_id)` 的模块函数。此前出过一个 `Step` 句柄，结果是它在
4 个包 16 个签名里流通，沿途每个使用者提一点要求，长成了 21 个成员的上帝类。

**不出目录、不出存储根**：目录 = 根 + 两级 id 的拼装公式，交出去等于把布局复制一份到调用方，
且门禁抓不到（`dir / "x"` 是普通 Path 拼接，不是 settings 访问）。要 ffmpeg 的 `cwd` 取
`scratch_path(...).parent` 或已定位文件的 `.parent`。
"""

from __future__ import annotations

import logging
import secrets
import shutil
from pathlib import Path
from typing import IO, Iterator, List, Optional, Tuple

from app.services.step_store import _layout

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 包内私有：目录定位
# ---------------------------------------------------------------------------


def _storage_root() -> Path:
    """存储根目录，直读 `settings.storage_base_dir` 单一真源。

    **包内私有**：它交出去就等于交出「根 + 两级 id + 文件名」的拼装能力，settings 那道门禁就
    白设了（拦属性访问拦不住 `from ...store import _storage_root`）。前导下划线与
    `test_import_hygiene.test_storage_root_is_private_to_step_store` 两道一起锁。

    settings 是 L3 故 import 写在函数体内；也正因为它是**函数**而非模块级常量，测试
    monkeypatch 后立即生效。
    """
    from app.settings import settings

    return settings.storage_base_dir


def _step_dir(task_id: int, step_id: int) -> Path:
    """该 step 的目录。**包内私有，绝不出包。** 不保证存在。"""
    return _storage_root() / _layout.step_subpath(task_id, step_id)


def _write_dir(task_id: int, step_id: int) -> Path:
    """写侧共用入口：保证 step 目录存在 + 刷新活动时间戳，返回 step 目录（**包内私有**）。

    活动时间戳是 TTL 的唯一判据（见 `_layout.ACTIVITY_NAME`）。挂在这里而不是让各写者自己
    记得 touch —— 写者能忘记调一个记账函数，但不可能忘记问包「往哪写」。

    touch 失败只记 debug 不抛：它失败意味着目录不可写，紧接着的真实写入会自己报错，没必要在
    这里替它抛一个更难懂的异常。
    """
    step_dir = _step_dir(task_id, step_id)
    step_dir.mkdir(parents=True, exist_ok=True)
    try:
        (step_dir / _layout.ACTIVITY_NAME).touch()
    except OSError as e:
        logger.debug("[StepStore] 活动标记刷新失败 %s: %s", step_dir, e)
    return step_dir


def _dir_name_to_int(name: str) -> Optional[int]:
    """目录名转 int；非数字（如 `.lab_exports`）返回 None。"""
    try:
        return int(name)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 文件出入口 —— 域自己拿着名字来
# ---------------------------------------------------------------------------


def file_path(task_id: int, step_id: int, name: str) -> Path:
    """该 step 目录下 `name` 这个文件该写到哪。**顺带建目录并刷新活动时间戳**（写侧入口）。

    `name` 由调用域自己持有（本模块不知道任何产物叫什么）。与 `find_file` 的分工：本函数收
    **调用方自己拥有的**名字，必返回一个路径（文件可以还不存在）；`find_file` 收**外部来的**
    名字，可能返回 None。
    """
    return _write_dir(task_id, step_id) / name


def open_file(
    task_id: int,
    step_id: int,
    name: str,
    mode: str = "r",
    *,
    encoding: Optional[str] = None,
) -> IO:
    """开该 step 目录下的一个文件，路径不出包。

    写模式（`w`/`a`/`x`）走 `_write_dir()`（建目录 + 刷活动时间戳）；**读模式两样都不做** ——
    读一个不存在的 step 不该在盘上留下空目录，也不该让它显得还活着（那样 TTL 按契约不删它，
    等于永久泄漏一个空目录）。文件不存在照常抛 `FileNotFoundError`。

    `encoding` 默认 utf-8；二进制模式忽略它。
    """
    base = (
        _write_dir(task_id, step_id)
        if any(c in mode for c in "wax")
        else _step_dir(task_id, step_id)
    )
    path = base / name
    if "b" in mode:
        return path.open(mode)
    return path.open(mode, encoding=encoding or "utf-8")


def find_file(task_id: int, step_id: int, filename: str) -> Optional[Path]:
    """按**外部给的**文件名取该 step 下的文件；不存在或越界返回 None。

    **path traversal 防御在这里，不在调用方**：`filename` 来自外部（媒体 token 的 payload），
    本函数保证解析后仍落在存储根内。不建目录、不刷活动时间戳。
    """
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        return None
    root = _storage_root()
    candidate = (_step_dir(task_id, step_id) / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def scratch_path(
    task_id: int, step_id: int, prefix: str, suffix: str = ".m3u8"
) -> Path:
    """step 目录内的临时文件路径（调用方负责建与删）。建目录但**不刷活动时间戳** —— 临时
    文件是读侧导出/打点的中间产物，不代表这个 step 还在产出。要 ffmpeg 的 `cwd` 取返回值的
    `.parent`。

    两条都是格式约束，不是风格：

    - **必须落在 step 目录** —— 临时 m3u8 里的 `EXT-X-MAP` 与段 URI 都是相对引用，只有与
      init/段同目录才解析得到。
    - **前导点命名** —— 前导点让它落在 `_layout.SEGMENT_PATTERN` 之外（该正则锚 `^`），半截
      的临时 mp4 才不会被段扫描当成一个真段列进 `hls.segments()`。新增临时文件沿用。
    """
    step_dir = _step_dir(task_id, step_id)
    step_dir.mkdir(parents=True, exist_ok=True)
    return step_dir / f".{prefix}_{secrets.token_hex(6)}{suffix}"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


def steps(task_id: Optional[int] = None) -> List[Tuple[int, int]]:
    """列出已落盘的 `(task_id, step_id)`，按升序。**只认两层数字目录名**，`.lab_exports` 等
    非任务目录跳过。

    Args:
        task_id: None 表示全局枚举（TTL 回收用）；给了则只列该 task 的。

    **出全部目录，不筛内容** —— 「有没有视频段」是 HLS 域的问题，问 `hls.list_steps()`。TTL
    恰恰要看见没有段的目录（只有 `features.jsonl` 的 step 就属于它），故它用本函数。
    """
    pairs = _iter_step_ids(_storage_root())
    if task_id is not None:
        return [p for p in pairs if p[0] == task_id]
    return list(pairs)


def _iter_step_ids(root: Path) -> Iterator[Tuple[int, int]]:
    """枚举 root 下全部两层数字目录名。"""
    if not root.is_dir():
        return
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir() or not task_dir.name.isdigit():
            continue
        try:
            children = sorted(task_dir.iterdir())
        except OSError as e:
            logger.debug("[StepStore] 无法读取 task 目录 %s: %s", task_dir, e)
            continue
        for child in children:
            if child.is_dir() and child.name.isdigit():
                yield int(task_dir.name), int(child.name)


def tasks(recent_first: bool = False) -> List[int]:
    """列存储根下的 task id。只认数字目录名（`.lab_exports` 等跳过）。

    Args:
        recent_first: 默认 False = 升序。True = 按「最近有段落盘」倒序，排序键取
            `max(该 task 下各 step 目录的 mtime)`。

    **`recent_first` 是廉价粗排，仅供挑深扫候选**：只 stat 目录不进目录，成本 O(目录数) 而非
    O(总段文件数)。近似是有意的 —— mtime 只决定「先深扫谁」，**绝不对外当时间戳用**，对外时间
    一律取 `hls.segments()` 的真实 ts_us。无 step 子目录的 task 排序键取 0，但仍在结果里。
    """
    base_dir = _storage_root()
    if not base_dir.is_dir():
        return []

    entries = [
        (entry, tid)
        for entry in base_dir.iterdir()
        if entry.is_dir() and (tid := _dir_name_to_int(entry.name)) is not None
    ]
    if not recent_first:
        return sorted(tid for _, tid in entries)

    keyed: List[Tuple[float, int]] = []
    for entry, task_id in entries:
        mtimes = [
            child.stat().st_mtime
            for child in entry.iterdir()
            if child.is_dir() and _dir_name_to_int(child.name) is not None
        ]
        keyed.append((max(mtimes) if mtimes else 0.0, task_id))
    keyed.sort(reverse=True)  # mtime 降序；同 mtime 时 task_id 大者优先
    return [task_id for _, task_id in keyed]


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def last_activity_at(task_id: int, step_id: int) -> Optional[float]:
    """最后一次有人要写东西的时刻（unix 秒），取 `_layout.ACTIVITY_NAME` 的 mtime；该文件
    不存在时 None（本判据上线之前建的目录就没有它，按下述契约不会被删）。

    记的是「有人问过往哪写」而不是「写成功了」——写入口一律先 touch 后返回路径。方向偏向保留
    数据，对 TTL 无害。

    供 TTL 判断「这目录还活着吗」，**「该不该删」不在本包**，在
    `persistence/workers/cleanup_worker.py`；None 时不删也是那边的决定。
    """
    try:
        return (_step_dir(task_id, step_id) / _layout.ACTIVITY_NAME).stat().st_mtime
    except OSError:
        return None


def purge_step(task_id: int, step_id: int) -> bool:
    """删除整个 step 目录，返回是否真的删了（不存在返回 False）。best-effort：失败只打
    warning 并返回 False，不抛。

    ⚠ **删的是整个目录含所有域的产物**：HLS 段与 playlist、`features.jsonl` / `facts.jsonl`、
    离线推理结果、活动标记一并消失。

    ⚠ **只执行删除，不判断该不该删** —— 保留策略在 `persistence/workers/cleanup_worker.py`，
    重启 supersede 的判断在 `persistence/strategies/hls_strategy.purge_step_dir`。

    ⚠ **调用方必须在本调用返回之后，才能创建本 run 的任何产物**：反过来，新建的
    `features.jsonl` 会被这里的 rmtree 抹掉且不报错。调用序见 `run_control.start_run`。
    """
    target = _step_dir(task_id, step_id)
    if not target.exists():
        return False
    try:
        shutil.rmtree(target)
        return True
    except OSError as e:
        logger.warning("[StepStore] 删除 step 目录失败 %s: %s", target, e)
        return False


def sweep_empty_tasks() -> int:
    """回收 step 全被抽走后留下的空 task 目录，返回删除个数。

    `rmdir` 对非空目录会安全失败，故无需先检查是否为空。
    """
    root = _storage_root()
    if not root.is_dir():
        return 0
    removed = 0
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir() or not task_dir.name.isdigit():
            continue
        try:
            task_dir.rmdir()
            removed += 1
            logger.info("[StepStore] 回收空 task 目录: %s", task_dir)
        except OSError:
            pass  # 非空或无权限，跳过
    return removed
