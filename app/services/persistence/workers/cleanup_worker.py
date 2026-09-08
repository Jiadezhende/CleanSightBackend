"""
存储 TTL 清理 Worker

**保留策略归本模块，落盘事实归 step_store。** 本模块决定「保留几天、多久扫一次、
哪些目录该扫、删不删」；「这目录最后何时活动」是 `Step.last_activity_at`、「怎么删」是
`step_store.store.purge_step`。两侧 docstring 互指。

职责：
- 后台 daemon 线程，定期扫描 database/{task_id}/{step_id}/
- 删除最后活动时间超过 cleanup_days 天的 step 目录（2026-05 起 step 为最小粒度）
- 顺手清空被全部 step 抽走后留下的空 task_id 目录
- 活跃 step 每 ~10s 落一个新段，产物 mtime 随之更新，不会被误删
"""

import logging
import threading
from datetime import datetime, timedelta

from app.services.step_store import store as step_store

logger = logging.getLogger(__name__)


class StorageCleanupWorker:
    """后台 TTL 清理 Worker"""

    def __init__(
        self,
        cleanup_days: int,
        interval_seconds: int = 3600,
        dry_run: bool = False,
    ):
        """
        **不收 db_dir**：存储根由 step_store 自解析（`settings.storage_base_dir`）。
        此前它由 `config.storage_base_dir` 传进来，而后者就是 `return
        settings.storage_base_dir` —— 同一个值绕两层，还把「路径」这个概念塞进了
        一个只该管保留策略的 worker。测试指向临时目录请 monkeypatch
        `settings.storage_dir`（`tmp_storage` fixture 即是）。

        Args:
            cleanup_days: 保留天数
            interval_seconds: 扫描周期
            dry_run: 只打印会删哪些 step、不真删。判据换代时先用它核对一轮
                （新判据会开始回收此前因缺 metadata.json 而漏扫的目录）。
        """
        self.cleanup_days = cleanup_days
        self.interval_seconds = interval_seconds
        self.dry_run = dry_run
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

        判定依据：`Step.last_activity_at`（step 目录内活动标记的 mtime，由 step_store
        的写入口顺带刷新）早于 cutoff。活跃 step 每 ~10s 落一个新段、每次都刷新它，故
        永远不会被误删。

        **判据两次换代**（2026-09）：最早看 `metadata.json.updated_at`，那是 HLS 独有
        产物，只有 `features.jsonl` 而没有 HLS 段的 step 永远扫不到、无限期堆积；随后
        改为「已登记产物 mtime 最大值」，代价是每新增一类产物都得记得登记它的 glob；
        现改为单一活动标记——写者只要问 step_store「往哪写」就已经记账，忘不了。

        `dry_run=True` 时只统计与打印、不真删——上线新判据前先用它核对会删哪些。
        """
        cutoff_ts = (datetime.now() - timedelta(days=self.cleanup_days)).timestamp()
        deleted = 0

        # 走 `store.steps()`（全部目录）而不是 `hls.list_steps()`（只有视频段的 step）：
        # 只有 features.jsonl 没有 HLS 段的目录正是此前泄漏的那一类，本 worker 必须看见它。
        for task_id, step_id in step_store.steps():
            last = step_store.last_activity_at(task_id, step_id)
            if last is None:
                # 没有活动标记：可能是刚建目录、只剩临时文件的残骸，也可能是本判据
                # 上线之前建的历史目录。不删——判不出是哪种，误删的代价高于留一个目录。
                logger.debug(
                    "[StorageCleanup] Skip step without activity marker: task=%s step=%s",
                    task_id, step_id,
                )
                continue
            if last >= cutoff_ts:
                continue

            when = datetime.fromtimestamp(last).isoformat(timespec="seconds")
            if self.dry_run:
                deleted += 1
                logger.info(
                    "[StorageCleanup][dry-run] Would delete: task=%s step=%s (last activity %s)",
                    task_id, step_id, when,
                )
                continue

            if step_store.purge_step(task_id, step_id):
                deleted += 1
                logger.info(
                    "[StorageCleanup] Deleted: task=%s step=%s (last activity %s)",
                    task_id, step_id, when,
                )

        # 顺手清理被掏空的 task_id 父目录
        empty_tasks = 0 if self.dry_run else step_store.sweep_empty_tasks()

        if deleted or empty_tasks:
            logger.info(
                "[StorageCleanup]%s Scan complete: %d step(s), %d empty task dir(s)",
                "[dry-run]" if self.dry_run else "",
                deleted, empty_tasks,
            )

        return deleted
