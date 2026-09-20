"""后台任务队列 —— 一条队列 + 一个消费线程，按提交顺序串行执行。

    q = SerialTaskQueue("hls")
    q.start()
    q.submit(lambda: hls.insert_segment(task_id, step_id, "raw", frames),
             label=f"seg:{task_id}/{step_id}/raw")
    q.submit(lambda: tasks.delete_step(task_id, step_id),
             label=f"purge:{task_id}/{step_id}", timeout=30.0)
    q.stop()

两条约束，破坏后都不报错，表现是回放跳段、旧段串进新 run：

- **不加第二个 worker。** 同 step 的「先落残段、再整个删」与相邻段 tfdt 单调，全靠提交序 == 执行序。
- **一条队列一个语义，谁用谁 new**，也不建全局注册表统一起停（停机顺序约束属于域）。

不做：重试（在 fn 里包 `GuardedExecutor`）、优先级、取消、结果回传、并行、跨进程。

依赖上界：stdlib。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)

__all__ = ["SerialTaskQueue"]

# 消费线程等待新任务的轮询间隔（秒）：停机信号最多延迟这么久才被看见。
_POLL_INTERVAL = 0.5


class _Task(NamedTuple):
    """队列元素。`label` 非可选：日志与丢弃告警的唯一线索。"""

    label: str
    fn: Callable[[], object]


class SerialTaskQueue:
    """单消费者任务队列：提交的任务按提交顺序、由同一个线程串行执行。

    **一次性**：`stop()` 之后不能再 `start()`，要重来就新建一个。
    """

    def __init__(self, name: str, maxsize: int = 100) -> None:
        self.name = name
        # 有界：满了丢任务（见 `submit`），不给无界选项。
        self._queue: queue.Queue[_Task] = queue.Queue(maxsize=maxsize)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """起消费线程。重复调用抛——两个消费线程就没有顺序可言。"""
        if self._thread is not None:
            raise RuntimeError(
                f"[{self.name}] SerialTaskQueue 是一次性的，已启动过不能再 start"
            )
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"SerialTaskQueue-{self.name}"
        )
        self._thread.start()
        logger.info("[%s] 任务队列已启动 (maxsize=%d)", self.name, self._queue.maxsize)

    def stop(self, timeout: float = 10.0) -> None:
        """置停机信号，等消费线程**排空后**退出。未 start 过是 no-op。

        排空是有意的：在队的任务持有已从上游拿走的数据（帧已从 CQ 弹出）。超时只记
        warning，线程是 daemon，剩下的任务随进程退出丢掉。
        """
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "[%s] 停机超时 (%.1fs)，仍有 %d 个任务未执行——进程退出时会被丢弃",
                self.name,
                timeout,
                self._queue.qsize(),
            )
        else:
            logger.info("[%s] 任务队列已停止", self.name)

    # ── 提交 ────────────────────────────────────────────────────────────────────

    def submit(
        self,
        fn: Callable[[], object],
        *,
        label: str,
        timeout: float = 1.0,
    ) -> bool:
        """提交无参任务，返回是否入队。False = 它不会被执行。

        `fn` 的返回值丢弃、异常记录后吞掉。`label` 无默认值：队列满时的唯一线索。

        ⚠ 默认 `timeout=1.0` 按「段写这类可丢任务」定。**不许丢的任务传大 timeout 并检查
        返回值**——purge 丢一个就是旧 run 的产物残留进新 run：

            if not q.submit(do_purge, label="purge:1/2", timeout=30.0):
                do_purge()      # 降级同步执行
        """
        if self._stop_event.is_set():
            logger.warning("[%s] 已停机，拒绝提交: %s", self.name, label)
            return False
        try:
            self._queue.put(_Task(label, fn), timeout=timeout)
            return True
        except queue.Full:
            logger.warning(
                "[%s] 队列已满 (%d)，丢弃任务: %s",
                self.name,
                self._queue.maxsize,
                label,
            )
            return False

    # ── 消费循环 ────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """消费线程主循环。不包 `guarded_run`：任务异常在 `_execute` 吞掉，本函数只有
        `queue.get`。
        """
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                continue
            self._execute(task)

        # 停机后排空。必然结束：`submit` 在 stop_event 置位后即拒收，无边排边进。
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            self._execute(task)

    def _execute(self, task: _Task) -> None:
        """执行单个任务。异常不出本函数——一个任务炸掉不能带走整条队列。"""
        try:
            task.fn()
        except Exception as e:
            logger.error(
                "[%s] 任务执行失败: %s - %s", self.name, task.label, e, exc_info=True
            )
