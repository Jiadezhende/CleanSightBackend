"""`app.utils.task_queue`：单消费者任务队列的顺序、隔离与停机语义。

断言集中在**这个类唯一卖点**上：提交顺序 == 执行顺序，且执行是串行的。HLS 落盘去掉目录锁
之后，「旧残段先落盘再整个 purge」「同轨相邻段 tfdt 不错位」两条正确性全部押在这上面——
这里松一寸，那边就是静默的回放跳段。

不碰文件系统，全部在内存里断言。
"""

import threading
import time

import pytest

from app.utils.task_queue import SerialTaskQueue


@pytest.fixture
def q():
    """每个用例一条新队列——SerialTaskQueue 是一次性的，不能跨用例复用。"""
    created = SerialTaskQueue("test", maxsize=8)
    yield created
    created.stop(timeout=5.0)  # 未 start 过或已 stop 过都是 no-op


# ---------------------------------------------------------------------------
# 核心保证：顺序 + 串行
# ---------------------------------------------------------------------------


def test_executes_in_submit_order(q):
    """提交顺序 == 执行顺序。这是本类存在的全部理由。"""
    done = []
    q.start()
    for i in range(20):
        assert q.submit(lambda i=i: done.append(i), label=f"t{i}")
    q.stop(timeout=5.0)

    assert done == list(range(20))


def test_tasks_never_overlap(q):
    """任意两个任务的执行区间不重叠——「串行」不只是有序，还要互不并发。

    用一个非重入的哨兵：任务进入时置位，退出时清零；若有重叠，第二个任务进来时哨兵还亮着。
    """
    overlapped = []
    running = threading.Event()

    def body():
        if running.is_set():
            overlapped.append(True)
        running.set()
        time.sleep(0.01)
        running.clear()

    q.start()
    for i in range(10):
        q.submit(body, label=f"t{i}")
    q.stop(timeout=5.0)

    assert not overlapped


def test_one_failing_task_does_not_kill_the_queue(q):
    """单个任务抛异常只被记录，后续任务照常执行——一个任务炸掉不能带走整条队列。"""
    done = []

    def boom():
        raise RuntimeError("boom")

    q.start()
    q.submit(lambda: done.append("before"), label="before")
    q.submit(boom, label="boom")
    q.submit(lambda: done.append("after"), label="after")
    q.stop(timeout=5.0)

    assert done == ["before", "after"]


# ---------------------------------------------------------------------------
# 停机：排空，然后拒收
# ---------------------------------------------------------------------------


def test_stop_drains_pending_tasks(q):
    """已提交的任务在停机时必须落地。

    它们代表已经从上游拿走的数据（HLS 段的帧已从 CQ 弹出），丢掉就是真丢——这正是现在
    `HLSWorker` 的缺陷：`while not stop_event.is_set()` 一置位就退出，队列里剩的段直接没。
    """
    done = []
    q.start()
    # 先塞一个慢任务占住消费线程，后面几个必然还在队列里排队时 stop 就会被调用
    q.submit(lambda: time.sleep(0.2), label="slow")
    for i in range(5):
        q.submit(lambda i=i: done.append(i), label=f"t{i}")

    q.stop(timeout=5.0)

    assert done == [0, 1, 2, 3, 4]


def test_submit_rejected_after_stop(q):
    """停机后拒收新任务——否则停机排空会与新提交赛跑，永远排不空。"""
    q.start()
    q.stop(timeout=5.0)

    assert q.submit(lambda: None, label="late") is False


def test_stop_without_start_is_noop(q):
    """没 start 过就 stop 不该炸：拆除路径经常在半初始化状态下走。"""
    q.stop(timeout=1.0)  # 不抛即可


# ---------------------------------------------------------------------------
# 满队列：丢弃是可见的，不是静默的
# ---------------------------------------------------------------------------


def test_submit_returns_false_when_full(q):
    """队列满时 `submit` 返回 False，且不会长期阻塞调用方。

    返回值是调用方唯一能知道「这个任务不会被执行」的途径——purge 这类不许丢的任务
    必须检查它。
    """
    # 不 start：没有消费者，队列必然填满
    for i in range(8):  # maxsize=8
        assert q.submit(lambda: None, label=f"t{i}", timeout=0.01) is True

    assert q.submit(lambda: None, label="overflow", timeout=0.01) is False


# ---------------------------------------------------------------------------
# 一次性
# ---------------------------------------------------------------------------


def test_double_start_raises(q):
    """重复 start 直接抛——静默忽略会让「起了两个消费线程」躺在那里，而两个线程就没有顺序了。"""
    q.start()
    with pytest.raises(RuntimeError):
        q.start()
