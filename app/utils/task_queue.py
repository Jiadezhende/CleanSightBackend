"""后台任务队列 —— 一条队列 + 一个消费线程，按提交顺序串行执行。

    from app.utils.task_queue import SerialTaskQueue

    q = SerialTaskQueue("hls")
    q.start()
    q.submit(lambda: hls.write_segment(task_id, step_id, "raw", frames),
             label=f"seg:{task_id}/{step_id}/raw")
    q.submit(lambda: step_tasks.delete_step(task_id, step_id),
             label=f"purge:{task_id}/{step_id}", timeout=30.0)
    q.stop()

## 为什么名字里有「串行」

它卖的不是「异步」，是**顺序**：提交进来的任务由唯一一个线程按提交顺序执行完一个再执行
下一个。HLS 落盘正是靠这条保证——同一 step 的段写与 purge 提交到同一条队列，于是
「旧残段先落盘 → 再整个删掉」是构造出来的，不需要任何锁；同一轨相邻段的 tfdt 也不会
因为提交乱序而与文件名 ts 轴错位。

叫「BackgroundTaskQueue」下一个人就会把它「优化」成多 worker，上面两条保证会**静默**
消失（不报错，只是回放跳段、旧段串进新 run）。名字是这里唯一挡得住的东西。

## 它不做什么

    重试            调用方在 fn 里自己包 GuardedExecutor —— 重试几次是策略，不是队列的事
    优先级 / 取消    没有需求；加了就得回答「插队会不会破坏顺序」，而顺序是本类唯一的卖点
    结果回传         提交即忘。要结果说明调用方在等，那它本就不该异步
    并行            一条队列一个线程。要并行就是另一条队列，别在这里加 worker
    跨进程           `threading` 只在本进程内有序

## 一条队列一个语义，谁用谁 new

不要把用途不同的任务混进同一条：HLS 那条承诺「同 step 有序」，告警那条只承诺「不阻塞
调用方」。混用会让前一条承诺被后一条的流量稀释——一次告警重试卡住 3 秒，段写就跟着等
3 秒。

也**不要**给它建一个全局注册表统一起停：停机顺序约束属于域而不属于队列（录制与告警都
必须停在 inference 之后），集中管理会把这些约束拉平成一条，然后就看不见了。谁需要谁 new
一条，在自己域的 lifespan 里 `start()` / `stop()`。

依赖上界：stdlib + `app.utils.worker_guard`。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, NamedTuple, Optional

from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)

__all__ = ["SerialTaskQueue"]

# 消费线程等待新任务的轮询间隔（秒）：停机信号最多延迟这么久才被看见。
_POLL_INTERVAL = 0.5


class _Task(NamedTuple):
    """队列元素。`label` 不是可选的——它是日志、丢弃告警、队列深度排查的唯一线索。"""

    label: str
    fn: Callable[[], object]


class SerialTaskQueue:
    """单消费者任务队列：提交的任务按提交顺序、由同一个线程串行执行。

    **一次性**：`stop()` 之后不能再 `start()`（`stop_event` 已置位）。要重来就新建一个。
    """

    def __init__(self, name: str, maxsize: int = 100) -> None:
        """
        Args:
            name: 队列名，出现在所有日志与线程名里。
            maxsize: 队列容量。满了之后 `submit` 的行为见该方法——**满队列是要被看见的**，
                所以不给无界队列的选项：无界只是把「丢一个任务」换成「吃光内存」。
        """
        self.name = name
        self._queue: queue.Queue[_Task] = queue.Queue(maxsize=maxsize)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """起消费线程。重复调用直接抛——静默忽略会让「起了两个线程」这种错躺在那里。"""
        if self._thread is not None:
            raise RuntimeError(
                f"[{self.name}] SerialTaskQueue 是一次性的，已启动过不能再 start"
            )
        thread_name = f"SerialTaskQueue-{self.name}"
        self._thread = threading.Thread(
            target=guarded_run,
            args=(self._run, self._stop_event, thread_name),
            daemon=True,
            name=thread_name,
        )
        self._thread.start()
        logger.info("[%s] 任务队列已启动 (maxsize=%d)", self.name, self._queue.maxsize)

    def stop(self, timeout: float = 10.0) -> None:
        """置停机信号并等消费线程**排空后**退出。未 start 过是 no-op。

        排空是有意的：已经提交的任务代表已经从上游拿走的数据（HLS 段的帧已从 CQ 弹出），
        丢掉就是真丢。`timeout` 到了仍没排完只记 warning——线程是 daemon，进程退出时
        会被杀，剩下的任务确实会丢，所以这条 warning 要能在日志里被找到。
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
        """提交一个任务，返回**是否入队成功**。

        Args:
            fn: 无参可调用对象。它的返回值被丢弃，异常被记录后吞掉（见 `_execute`）。
                要重试就在 fn 内部自己包 `GuardedExecutor`。
            label: 任务标识，用于日志与丢弃告警。keyword-only 且无默认值——排查
                「队列为什么满了」时，没有 label 就只能看到一串匿名 lambda。
            timeout: 队列满时最多等多久。

        Returns:
            False 表示**任务没有进队列，它不会被执行**。

        ⚠ **不许丢的任务必须显式传大 timeout 并检查返回值。** 典型例子是 purge：段写丢一个
        只是少一段录像，purge 丢一个就是旧 run 的产物残留进新 run。默认值 1.0 是按「段写
        这类可丢任务」定的，漏传就是 best-effort，这在 purge 上是错的：

            if not q.submit(do_purge, label="purge:1/2", timeout=30.0):
                do_purge()      # 降级同步执行，宁可阻塞调用方也不能不删
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

    # ── 观测 ────────────────────────────────────────────────────────────────────

    def qsize(self) -> int:
        """当前排队长度（近似值，仅供日志与压力观测，别拿它做控制流判断）。"""
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── 消费循环 ────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """消费线程主循环。由 `guarded_run` 包裹：本函数意外抛出时会被重启。"""
        logger.debug("[%s] 消费线程启动", self.name)

        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                continue
            self._execute(task)

        # 停机后排空。**这个循环一定会结束**：`submit` 在 stop_event 置位后即拒收，
        # 不存在边排边进的赛跑。
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            self._execute(task)

        logger.debug("[%s] 消费线程退出", self.name)

    def _execute(self, task: _Task) -> None:
        """执行单个任务。**异常不出这个函数**——一个任务炸掉不能带走整条队列。

        这是边界层捕获的第 5 个位置（见 `app/utils/BOUNDARY_LAYER_EXAMPLES.md` 的四个
        边界层）：语义与 `Worker.run()` 同款，只是被提交进来的活是个闭包而不是一个方法。
        """
        try:
            task.fn()
        except Exception as e:
            logger.error(
                "[%s] 任务执行失败: %s - %s", self.name, task.label, e, exc_info=True
            )
