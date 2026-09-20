"""
存储 TTL 清理 Worker

职责：
- 后台 daemon 线程，定期扫描 database/{task_id}/{step_id}/ 目录
- 删除**目录自身 mtime** 超过 cleanup_days 天的 step 目录（2026-05 起 step 为最小粒度）
- 顺手清空被全部 step 抽走后留下的空 task_id 目录

判据为什么是 step 目录自身的 mtime，见 `_scan_and_clean` 的 docstring。
"""

import logging
import shutil
import threading
import time
from pathlib import Path

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

        **判据 = `{task}/{step}/` 目录自身的 `st_mtime`，不下钻域子目录**，超过
        `cleanup_days` 天即删。

        ## 为什么不再读 `metadata.json` 的 `updated_at`

        产物已按域隔离（`{step}/hls/` / `features/` / `lab/`），`metadata.json` 随之落进
        `{step}/hls/`。原来的 `glob("*/*/metadata.json")` 匹配不到它，表现是**新数据永不
        回收、老数据照常回收**——单向漏盘且无任何日志。改用目录 mtime 后对平铺与分域两种
        布局一视同仁，同时消解「只有 features.jsonl、没有 HLS 段的 step 永不回收」那类泄漏。

        ## 为什么 `{step}` 的 mtime 是创建时间的好代理

        `{step}/` 的直接子项只有 `hls/` / `features/` / `lab/` 三个域目录，目录 mtime 只在
        **增删直接子项**时变。段落盘动的是 `hls/` 的 mtime，`{step}/` 纹丝不动。
        （Linux + Python 3.11 拿不到真正的创建时间：`st_birthtime` 不存在，`st_ctime` 两平台
        语义不同，都不能当创建时间用。）

        ## ⚠ 两条已知偏差（是设计后果，不是漂移，别当 bug 查）

        1. **`{step}/lab/` 是延迟创建的**：某个 step 第一次被导出送标时才建这个子目录，那一刻
           `{step}/` 的 mtime 被刷新一次，该 step 的 TTL 计时重置。算白捡的续命——正在被反复
           导出的 step 天然不被回收。
        2. **活跃 step 不再免疫**：旧判据下 `updated_at` 每 ~10s 刷新一次，跑着的 step 永远删
           不掉；换判据后，一个连续跑满 `cleanup_days`（当前 15 天）的 step 会被删掉自己正在
           写的录像。当前任务超时 30 分钟、触发不到，但**把 `cleanup_days` 调小或引入长跑任务
           时会真的发生**。

        ## ⚠ 不要改成复用 `app.storage.tasks.list_task_ids(order="mtime")`

        那个口径**下钻域子目录取最大值**，答的是「最近活动」不是「创建」——每写一段就续一次
        命，等于永不回收。两个口径分开是刻意的，`tasks.py` 的 docstring 也写着这一点。
        """
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

            try:
                shutil.rmtree(step_dir)
                deleted += 1
                logger.info("[StorageCleanup] Deleted step dir: %s", step_dir)
            except OSError as e:
                logger.warning("[StorageCleanup] Failed to delete %s: %s", step_dir, e)

        # 顺手清理被掏空的 task_id 父目录（仅删空目录，rmdir 对非空目录会安全失败）
        empty_tasks = 0
        for task_dir in self._iterdir(self.db_dir):
            if not task_dir.is_dir():
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
        扫描）。旧判据靠「有没有 `metadata.json`」把它天然挡在外面，换成目录 mtime 后必须由
        这里显式挡——否则一个 15 天没动过的导出临时目录会被当成过期 step 删掉。

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
