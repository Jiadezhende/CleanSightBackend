"""
存储 TTL 清理 Worker

职责：
- 后台 daemon 线程，定期扫描 database/{task_id}/{step_id}/metadata.json
- 删除 updated_at 超过 cleanup_days 天的 step 目录（2026-05 起 step 为最小粒度）
- 顺手清空被全部 step 抽走后留下的空 task_id 目录
- 活跃 step 每 ~10s 更新一次 updated_at，不会被误删
"""

import json
import logging
import shutil
import threading
from datetime import datetime, timedelta
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

        判定依据：metadata.json 中 updated_at 超过 cleanup_days 天。
        活跃 step 每 ~10s 更新一次 updated_at，永远不会被误删。
        """
        cutoff = datetime.now() - timedelta(days=self.cleanup_days)
        deleted = 0

        for metadata_path in self.db_dir.glob("*/*/metadata.json"):
            step_dir = metadata_path.parent
            try:
                with metadata_path.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (IOError, json.JSONDecodeError) as e:
                logger.debug("[StorageCleanup] Skip unreadable metadata %s: %s", metadata_path, e)
                continue

            raw = meta.get("updated_at", "")
            try:
                updated_at = datetime.fromisoformat(str(raw))
            except (ValueError, TypeError):
                logger.debug("[StorageCleanup] Skip invalid updated_at in %s", metadata_path)
                continue

            if updated_at >= cutoff:
                continue

            try:
                shutil.rmtree(step_dir)
                deleted += 1
                logger.info("[StorageCleanup] Deleted step dir: %s", step_dir)
            except OSError as e:
                logger.warning("[StorageCleanup] Failed to delete %s: %s", step_dir, e)

        # 顺手清理被掏空的 task_id 父目录（仅删空目录，rmdir 对非空目录会安全失败）
        empty_tasks = 0
        for task_dir in self.db_dir.iterdir():
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
