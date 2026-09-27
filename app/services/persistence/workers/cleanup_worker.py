"""
存储 TTL 清理 Worker

职责（每轮依次）：
- 清空回收区 `{db_dir}/.trash/`（上一轮 rmtree 没删掉的残留）
- 删除**目录自身 mtime** 超过 cleanup_days 天的 step 目录（2026-05 起 step 为最小粒度），
  删除走 `app.storage._fs.remove`（先 rename 进回收区，原子）
- 顺手清空被全部 step 抽走后留下的空 task_id 目录（只认数字目录名）

判据为什么是 step 目录自身的 mtime，见 `_scan_and_clean` 的 docstring。
"""

import logging
import threading
import time
from pathlib import Path

from app.storage import _fs

logger = logging.getLogger(__name__)


class StorageCleanupWorker:
    """后台 TTL 清理 Worker"""

    def __init__(
        self,
        db_dir: Path,
        cleanup_days: int,
        interval_seconds: int = 3600,
    ):
        self.db_dir = db_dir
        self.cleanup_days = cleanup_days
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="StorageCleanup"
        )
        self._thread.start()
        logger.info(
            "[StorageCleanup] Started, interval=%ds, retention=%dd",
            self.interval_seconds,
            self.cleanup_days,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # 首次等待一个 interval，避免启动时立即扫描
        while not self._stop_event.wait(timeout=self.interval_seconds):
            try:
                self._scan_and_clean()
            except Exception:
                # L1 边界层：捕获扫描中一切未预期异常，记录后继续下一轮
                # 不使用 GuardedExecutor（L2），因为此处需要的是线程存活而非立即重试
                logger.exception("[StorageCleanup] Unexpected error during scan, will retry next interval")

    def _scan_and_clean(self) -> int:
        """扫描并删除过期 step 目录 + 清空 task_id 父目录，返回删除的 step 数量。

        **判据 = `{task}/{step}/` 目录自身的 `st_mtime`，不下钻**，超过 `cleanup_days` 天即删，
        整个 step（含其下所有 run）一起删，不区分 run。

        `{step}/` 的直接子项只有 run 目录，目录 mtime 只在增删直接子项时变，所以它就是最近一次
        `runs.allocate` 的时刻：TTL 从该 step 最后一次开跑算起。段与检测结果落在
        `{step}/{run_id}/{domain}/`，写入不刷新它。旧布局残留（`{step}/hls/` 等）同样按这个
        判据随 step 回收。

        ⚠ **活跃 step 不免疫**：一个连续跑满 `cleanup_days` 的 run 会被删掉自己正在写的目录（写者
        随即 `FileNotFoundError`）。任务超时远短于 `cleanup_days`，触发不到；调小 `cleanup_days`
        或引入长跑任务时要重新评估。
        """
        _fs.purge_trash(root=self.db_dir)

        cutoff = time.time() - self.cleanup_days * 86400
        deleted = 0

        for step_dir in self._iter_step_dirs():
            try:
                mtime = step_dir.stat().st_mtime
            except OSError as e:
                # 扫描期间被删 / 不可读：等同于没扫到
                logger.debug("[StorageCleanup] Skip unreadable step dir %s: %s", step_dir, e)
                continue

            if mtime >= cutoff:
                continue

            # FAILED 时 `_fs.remove` 已记 warning，盘上原样不动，下一轮再试
            if _fs.remove(step_dir, root=self.db_dir) is _fs.Removed.REMOVED:
                deleted += 1
                logger.info("[StorageCleanup] Deleted step dir: %s", step_dir)

        # 顺手清理被掏空的 task_id 父目录（仅删空目录，rmdir 对非空目录会安全失败）。
        # 只认数字目录名：`.trash/`、`.lab_exports/` 空着也不归这里删
        empty_tasks = 0
        for task_dir in self._iterdir(self.db_dir):
            if not task_dir.is_dir() or not task_dir.name.isdigit():
                continue
            try:
                next(task_dir.iterdir())
            except StopIteration:
                try:
                    task_dir.rmdir()
                    empty_tasks += 1
                    logger.info("[StorageCleanup] Removed empty task dir: %s", task_dir)
                except OSError as e:
                    logger.debug("[StorageCleanup] Skip non-removable empty task dir %s: %s", task_dir, e)
            except OSError as e:
                logger.debug("[StorageCleanup] Skip unreadable task dir %s: %s", task_dir, e)

        if deleted or empty_tasks:
            logger.info(
                "[StorageCleanup] Scan complete: deleted %d step(s), %d empty task dir(s)",
                deleted, empty_tasks,
            )

        return deleted

    def _iter_step_dirs(self):
        """`{db_dir}/{task_id}/{step_id}/` 全体，**两级都只认十进制数字目录名**。

        数字过滤不是洁癖：存储根下还住着 `.lab_exports/`（lab 导出临时件，自带 30 分钟孤儿
        扫描）与 `.trash/`（回收区）。判据只看目录 mtime，不由这里显式挡，一个 15 天没动过的
        导出临时目录就会被当成过期 step 删掉。

        **不复用 `app.storage.tasks.list_step_ids`**：本 worker 扫的是注入的 `self.db_dir`，
        而 `tasks`/`_root` 的路径一律从 `settings` 解析——两者在生产同源，但让删除动作认一个
        它自己没扫过的根，是不必要的错位风险。
        """
        for task_dir in self._iterdir(self.db_dir):
            if not task_dir.is_dir() or not task_dir.name.isdigit():
                continue
            for step_dir in self._iterdir(task_dir):
                if step_dir.is_dir() and step_dir.name.isdigit():
                    yield step_dir

    @staticmethod
    def _iterdir(directory: Path):
        """遍历目录；不存在 / 不可读时产出空序列。"""
        try:
            yield from directory.iterdir()
        except OSError as e:
            logger.debug("[StorageCleanup] Skip unreadable dir %s: %s", directory, e)
