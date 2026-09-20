"""`app.services.recording`：代次校验（乐观锁）+ 打包落盘任务 + 懒惰 supersede。

格式怎么落盘不在这里测（那是 `test_storage_hls.py`）。这里只钉**编排**的三件事：

1. **代次校验**——哪些段该写、哪些该丢。判据是「同一个 `(task, step)` 上是不是换了新
   CQ」；换了 step 的新 CQ 写的是另一个目录，盘上不冲突，旧那批**照常落盘**。
2. **懒惰 supersede**——本代次首写时清掉上一代，且**只有自己还注册着**时才清。少了后
   半句，拆除之后才执行的残段会把刚写完的整段录像删掉。
3. **打包与拒收**——`submit_segment` 从 cq 派生分区键，三种情况直接拒收不入队。

主体用例把队列换成同步执行的替身、把 `hls` 换成记录调用的替身，因此不碰线程、不碰
cv2/ffmpeg，全部毫秒级。末尾一条端到端跑真队列 + 真 `storage.hls`，缺外部工具时 skip。
"""

from pathlib import Path

import pytest

import factories
from app.services.recording import service as recording_service_module
from app.services.recording._sweeper import SegmentSweeper
from app.services.recording.config import RecordingConfig
from app.services.recording.service import RecordingService
from app.settings import settings


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class FakeCQ:
    """够用的 CQ：身份三件套 + 取段 / drain 残帧。"""

    def __init__(self, task_id=1, step_id=2, ca_segment_len=3, name="cq"):
        self.task_id = task_id
        self.step_id = step_id
        self.ca_segment_len = ca_segment_len
        self.name = name
        self.raw_segments = []          # take_raw_segment 依次弹出
        self.processed_segments = []
        self.raw_residual = []          # drain_ca_raw 一次性取走
        self.processed_residual = []

    def __repr__(self):                 # 让失败信息可读
        return f"<FakeCQ {self.name} task={self.task_id} step={self.step_id}>"

    def take_raw_segment(self):
        return self.raw_segments.pop(0) if self.raw_segments else None

    def take_processed_segment(self):
        return self.processed_segments.pop(0) if self.processed_segments else None

    def drain_ca_raw(self, until_ts=None):
        self.raw_residual, out = _split_at_fence(self.raw_residual, until_ts)
        return out

    def drain_ca_processed(self, until_ts=None):
        self.processed_residual, out = _split_at_fence(self.processed_residual, until_ts)
        return out


def _split_at_fence(residual, until_ts):
    """复刻 `ClientQueues.drain_ca_*` 的栅栏语义：`None` 全取，否则只取队首前缀。

    返回 `(留下的, 取走的)`。
    """
    if until_ts is None:
        return [], list(residual)
    cut = 0
    while cut < len(residual) and residual[cut].timestamp <= until_ts:
        cut += 1
    return list(residual[cut:]), list(residual[:cut])


class FakeClients:
    """CQ 注册表：代次的唯一真源。"""

    def __init__(self, registry=None):
        self.registry = dict(registry or {})

    def get(self, task_id):
        return self.registry.get(task_id)

    def snapshot(self):
        return dict(self.registry)


class FakeHls:
    """记录 `delete` / `insert_segment` 的调用序。"""

    def __init__(self):
        self.calls = []

    def ts_to_us(self, ts):             # `_SegmentJob.label` 用得到
        return int(ts * 1e6)

    def delete(self, task_id, step_id):
        self.calls.append(("delete", task_id, step_id))
        return True

    def insert_segment(self, task_id, step_id, track, frames):
        self.calls.append(("insert", task_id, step_id, track, len(frames)))

    @property
    def deletes(self):
        return [c for c in self.calls if c[0] == "delete"]

    @property
    def inserts(self):
        return [c for c in self.calls if c[0] == "insert"]


class InlineQueue:
    """同步执行的队列替身：`submit` 当场把任务跑完，测试不必等线程。

    提交序即执行序这一点与真队列一致（都是 FIFO 单线程语义），所以用它测代次逻辑是
    等价的；真队列的线程行为另有一条用例覆盖。
    """

    def __init__(self, accept=True):
        self.accept = accept
        self.labels = []

    def submit(self, fn, *, label, timeout=1.0):
        self.labels.append(label)
        if not self.accept:
            return False
        fn()
        return True


@pytest.fixture
def fake_hls(monkeypatch):
    stub = FakeHls()
    monkeypatch.setattr(recording_service_module, "hls", stub)
    return stub


def _service(clients, *, accept=True) -> RecordingService:
    """一个装好同步队列的服务（不起线程）。"""
    svc = RecordingService(config=RecordingConfig(), clients=clients)
    svc._queue = InlineQueue(accept=accept)
    return svc


def _frames(n=3, start=1700.0):
    return [factories.make_frame(ts=start + i / 15.0) for i in range(n)]


# ---------------------------------------------------------------------------
# 代次校验（乐观锁）
# ---------------------------------------------------------------------------


class TestGenerationCheck:
    def test_same_partition_new_cq_discards_the_old_generation(self, fake_hls):
        """同一个 (task, step) 上换了新 CQ → 手上这批属于上一代，丢弃。"""
        old = FakeCQ(1, 2, name="A")
        new = FakeCQ(1, 2, name="B")
        svc = _service(FakeClients({1: new}))

        assert svc.submit_segment(old, "raw", _frames()) is True   # 入队成功
        assert fake_hls.calls == []                                 # 但执行时被丢弃

    def test_step_switch_does_not_conflict_on_disk_so_it_still_writes(self, fake_hls):
        """换的是 step 不是同一块盘 —— 上一步那批段照常落盘，且不清任何目录。

        注册表按 task_id 索引，只比 task_id 的话，「切下一步」会把 stop_run 刚交出来的
        上一步残段全当成过期丢掉，而它们既不覆盖谁也不被谁覆盖。
        """
        step2 = FakeCQ(1, 2, name="A")
        step3 = FakeCQ(1, 3, name="B")
        svc = _service(FakeClients({1: step3}))

        svc.submit_segment(step2, "raw", _frames())

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 3)]
        assert fake_hls.deletes == []

    def test_unregistered_cq_still_writes(self, fake_hls):
        """CQ 已出注册表（拆除后的残段）→ 没人接管这块盘，照常落盘、不清目录。"""
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({}))

        svc.submit_segment(cq, "raw", _frames())

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 3)]
        assert fake_hls.deletes == []

    def test_other_task_in_registry_is_irrelevant(self, fake_hls):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({9: FakeCQ(9, 2)}))

        svc.submit_segment(cq, "raw", _frames())

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 3)]


# ---------------------------------------------------------------------------
# 懒惰 supersede
# ---------------------------------------------------------------------------


class TestLazySupersede:
    def test_first_write_of_a_generation_deletes_then_inserts(self, fake_hls):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))

        svc.submit_segment(cq, "raw", _frames())

        assert fake_hls.calls == [("delete", 1, 2), ("insert", 1, 2, "raw", 3)]

    def test_second_write_does_not_delete_again(self, fake_hls):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))

        svc.submit_segment(cq, "raw", _frames(start=1700.0))
        svc.submit_segment(cq, "processed", _frames(start=1700.0))
        svc.submit_segment(cq, "raw", _frames(start=1710.0))

        assert len(fake_hls.deletes) == 1        # 整个 step 只清这一次
        assert len(fake_hls.inserts) == 3

    def test_straggler_after_forget_task_never_deletes(self, fake_hls):
        """**这条是防炸的。**

        拆除之后才执行的残段：注册表里已经没有它、代次表也被 `forget_task` 清了。若规则
        是「表里不是我就清」，它会把这个 step 刚写完的整段录像删掉再写。
        """
        cq = FakeCQ(1, 2)
        clients = FakeClients({1: cq})
        svc = _service(clients)

        svc.submit_segment(cq, "raw", _frames(start=1700.0))     # 活着时首写：清一次
        clients.registry.clear()                                  # 拆除
        svc.forget_task(1)                                        # 代次表回收（走队列）
        svc.submit_segment(cq, "raw", _frames(start=1710.0))      # 迟到的残段

        assert len(fake_hls.deletes) == 1                         # 没有第二次 delete
        assert len(fake_hls.inserts) == 2

    def test_restart_supersedes_and_discards_the_late_segment(self, fake_hls):
        """完整剧本：A 写过 → 同 step 起 B → B 首写清掉 A → A 的迟到段被丢弃。"""
        a = FakeCQ(1, 2, name="A")
        b = FakeCQ(1, 2, name="B")
        clients = FakeClients({1: a})
        svc = _service(clients)

        svc.submit_segment(a, "raw", _frames(start=1700.0))
        clients.registry[1] = b                                   # 重启换代
        svc.submit_segment(b, "raw", _frames(start=1800.0))       # B 首写 → 清 A
        svc.submit_segment(a, "raw", _frames(start=1710.0))       # A 的迟到段

        assert fake_hls.calls == [
            ("delete", 1, 2), ("insert", 1, 2, "raw", 3),         # A 那一代
            ("delete", 1, 2), ("insert", 1, 2, "raw", 3),         # B 接管
        ]                                                          # A 的迟到段不在里面

    def test_each_step_claims_independently(self, fake_hls):
        """代次表按 (task, step) 记 —— 同一个 CQ 不可能同时写两个 step，但换步之后
        新 step 是一次独立的首写。"""
        step2 = FakeCQ(1, 2, name="A")
        step3 = FakeCQ(1, 3, name="B")
        clients = FakeClients({1: step2})
        svc = _service(clients)

        svc.submit_segment(step2, "raw", _frames())
        clients.registry[1] = step3
        svc.submit_segment(step3, "raw", _frames())

        assert fake_hls.deletes == [("delete", 1, 2), ("delete", 1, 3)]


# ---------------------------------------------------------------------------
# 代次表回收
# ---------------------------------------------------------------------------


class TestForgetTask:
    def test_only_touches_that_task(self, fake_hls):
        a, b, other = FakeCQ(1, 2), FakeCQ(1, 3), FakeCQ(9, 2)
        clients = FakeClients({1: a, 9: other})
        svc = _service(clients)

        svc.submit_segment(a, "raw", _frames())
        clients.registry[1] = b
        svc.submit_segment(b, "raw", _frames())
        svc.submit_segment(other, "raw", _frames())

        assert svc.forget_task(1) is True
        assert list(svc._claimed_by) == [(9, 2)]

    def test_goes_through_the_queue(self, fake_hls):
        """走队列而不是当场清 —— `_claimed_by` 因此只被队列那一个线程碰，不需要锁。"""
        svc = _service(FakeClients({}))
        svc.forget_task(7)
        assert svc._queue.labels == ["forget:7"]

    def test_runs_after_the_segments_it_follows(self, fake_hls):
        """FIFO：记录活到这一代最后一段写完才消失，而不是在残段还没落盘时就被抹掉。"""
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))

        svc.submit_segment(cq, "raw", _frames(start=1700.0))
        svc.forget_task(1)

        assert svc._queue.labels == ["seg:1/2/raw@1700000000", "forget:1"]
        assert svc._claimed_by == {}
        assert len(fake_hls.deletes) == 1        # 段是在记录还在时写的，没被重复清

    def test_unknown_task_is_a_noop(self, fake_hls):
        svc = _service(FakeClients({}))
        assert svc.forget_task(404) is True      # 排进去了，执行时发现无事可清
        assert svc._claimed_by == {}

    def test_queue_not_started(self, fake_hls):
        svc = RecordingService(config=RecordingConfig(), clients=FakeClients({}))
        assert svc.forget_task(1) is False

    def test_full_queue_leaks_one_record_but_does_not_raise(self, fake_hls):
        """排不进去只是漏一条小壳记录，下一代首写会覆盖它 —— 不降级同步执行。"""
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))
        svc.submit_segment(cq, "raw", _frames())
        svc._queue.accept = False

        assert svc.forget_task(1) is False
        assert list(svc._claimed_by) == [(1, 2)]


# ---------------------------------------------------------------------------
# 打包与拒收
# ---------------------------------------------------------------------------


class TestSubmitRejections:
    def test_queue_not_started(self, fake_hls):
        svc = RecordingService(config=RecordingConfig(), clients=FakeClients({}))
        assert svc.submit_segment(FakeCQ(), "raw", _frames()) is False
        assert fake_hls.calls == []

    def test_empty_frames_never_reach_the_queue(self, fake_hls):
        """空段在 storage 层是 ValueError —— 在入口拦掉，别让它变成队列里的 error log。"""
        svc = _service(FakeClients({}))
        assert svc.submit_segment(FakeCQ(), "raw", []) is False
        assert svc._queue.labels == []

    @pytest.mark.parametrize("task_id,step_id", [(None, 2), (1, None), (None, None)])
    def test_cq_without_partition_key(self, fake_hls, task_id, step_id):
        svc = _service(FakeClients({}))
        cq = FakeCQ(task_id, step_id)
        assert svc.submit_segment(cq, "raw", _frames()) is False
        assert svc._queue.labels == []

    def test_full_queue_returns_false_and_writes_nothing(self, fake_hls):
        svc = _service(FakeClients({}), accept=False)
        assert svc.submit_segment(FakeCQ(), "raw", _frames()) is False
        assert fake_hls.calls == []

    def test_label_identifies_the_exact_segment(self, fake_hls):
        svc = _service(FakeClients({}))
        svc.submit_segment(FakeCQ(1, 2), "raw", _frames(start=1700.0))
        assert svc._queue.labels == ["seg:1/2/raw@1700000000"]


# ---------------------------------------------------------------------------
# 残段收尾
# ---------------------------------------------------------------------------


class TestFlushResidual:
    def test_slices_by_ca_segment_len(self, fake_hls):
        cq = FakeCQ(1, 2, ca_segment_len=2)
        cq.raw_residual = _frames(5)
        cq.processed_residual = _frames(3)
        svc = _service(FakeClients({}))

        svc.flush_residual(cq)

        assert fake_hls.inserts == [
            ("insert", 1, 2, "raw", 2),
            ("insert", 1, 2, "raw", 2),
            ("insert", 1, 2, "raw", 1),          # 最后一截不足一段也要落
            ("insert", 1, 2, "processed", 2),
            ("insert", 1, 2, "processed", 1),
        ]

    def test_empty_residual_submits_nothing(self, fake_hls):
        svc = _service(FakeClients({}))
        svc.flush_residual(FakeCQ(1, 2))
        assert svc._queue.labels == []

    def test_cq_without_partition_key_is_skipped(self, fake_hls):
        cq = FakeCQ(1, None)
        cq.raw_residual = _frames(3)
        svc = _service(FakeClients({}))

        svc.flush_residual(cq)

        assert svc._queue.labels == []

    def test_residual_carries_the_generation(self, fake_hls):
        """残段带的是**自己那一代**的身份：拆除后注册表已换人也不会串台。"""
        old = FakeCQ(1, 2, ca_segment_len=2, name="A")
        old.raw_residual = _frames(2)
        svc = _service(FakeClients({1: FakeCQ(1, 2, name="B")}))

        svc.flush_residual(old)

        assert fake_hls.calls == []          # 同分区已换代 → 丢弃，不是写成 B 的段

    def test_fence_leaves_post_reconnect_frames_in_the_queue(self, fake_hls):
        """断流 flush 只切栅栏之前的帧。

        全排空的后果是静默的：重连后的帧会跟断流前的帧拼进同一段，该段 `eff_fps` 由首末帧
        跨度反推、跨度里混着整个 gap → 慢放。栅栏取"断流前最后一帧 ts"，正是那个切点。
        """
        cq = FakeCQ(1, 2, ca_segment_len=10)
        before = _frames(3, start=1700.0)            # 断流前
        after = _frames(2, start=1720.0)             # 重连后（gap 20s）
        cq.raw_residual = before + after
        cq.processed_residual = list(before)
        svc = _service(FakeClients({}))

        svc.flush_residual(cq, until_ts=before[-1].timestamp)

        assert fake_hls.inserts == [
            ("insert", 1, 2, "raw", 3),
            ("insert", 1, 2, "processed", 3),
        ]
        assert cq.raw_residual == after, "重连后的帧必须留在队列里，等 sweeper 照常拉整段"


class TestCollectFrom:
    """运行期取帧的唯一入口：拉两轨整段 → 断流残帧，顺序固定。

    这几条原先挂在 `TestSweeper` 上。逻辑从定时器搬回 service 之后，用例跟着搬——断言留在
    定时器那边就等于承认定时器该懂这套顺序。
    """

    def test_pulls_both_tracks_in_order(self, fake_hls):
        cq = FakeCQ(1, 2, name="A")
        cq.raw_segments = [_frames(3), _frames(3)]
        cq.processed_segments = [_frames(2)]
        svc = _service(FakeClients({}))

        svc.collect_from(cq)

        assert fake_hls.inserts == [
            ("insert", 1, 2, "raw", 3),
            ("insert", 1, 2, "raw", 3),
            ("insert", 1, 2, "processed", 2),
        ]

    def test_cq_without_step_id_keeps_its_frames_buffered(self, fake_hls):
        """定位不到落盘分区就**不取帧** —— 取了只能丢，留在缓冲里等它绑上 step 才对。"""
        cq = FakeCQ(1, None)
        cq.raw_segments = [_frames(3)]
        svc = _service(FakeClients({}))

        svc.collect_from(cq)

        assert fake_hls.calls == []
        assert len(cq.raw_segments) == 1

    def test_pending_flush_runs_after_the_full_segments_on_both_tracks(self, fake_hls):
        """残段的 ts 晚于本轮所有整段，**必须后提交**——反过来清单 ts 就逆序了。

        入队序即执行序，而每段的 tfdt = 执行时读到的累计 EXTINF：顺序一乱，后写的段会在
        媒体轴上盖掉先写的，不报错、只是画面丢一截。

        **两轨都要断言**：只给 raw 的话，把残帧那步挪到两个 `while` 之间（raw 整段之后、
        processed 整段之前）照样绿，而那样 processed 清单就逆序了。
        """
        cq = FakeCQ(1, 2, ca_segment_len=10, name="A")
        cq.raw_segments = [_frames(10, start=1700.0)]
        cq.processed_segments = [_frames(10, start=1700.0)]
        cq.raw_residual = _frames(4, start=1701.0)
        cq.processed_residual = _frames(4, start=1701.0)
        svc = _service(FakeClients({}))
        svc.request_residual_flush(cq, fence_ts=1709.0)

        svc.collect_from(cq)

        assert fake_hls.inserts == [
            ("insert", 1, 2, "raw", 10),                   # 先把两轨的整段拉完
            ("insert", 1, 2, "processed", 10),
            ("insert", 1, 2, "raw", 4),                    # 残段才轮到
            ("insert", 1, 2, "processed", 4),
        ]

    def test_no_pending_request_means_only_full_segments(self, fake_hls):
        cq = FakeCQ(1, 2, ca_segment_len=10, name="A")
        cq.raw_segments = [_frames(10)]
        cq.raw_residual = _frames(4)
        svc = _service(FakeClients({}))

        svc.collect_from(cq)

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 10)]
        assert len(cq.raw_residual) == 4, "没有断流请求时残帧照旧留在缓冲里等攒满"

    def test_pending_request_is_consumed_once(self, fake_hls):
        """挂起请求是一次性的：下一轮 sweep 不该再切一遍。"""
        cq = FakeCQ(1, 2, ca_segment_len=10, name="A")
        cq.raw_residual = _frames(4, start=1700.0)
        svc = _service(FakeClients({}))
        svc.request_residual_flush(cq, fence_ts=1709.0)

        svc.collect_from(cq)
        svc.collect_from(cq)

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 4)]


class TestPendingFlushRequest:
    """断流 flush 的登记 / 取走 —— 生产者是 health_monitor 线程，消费者是 sweeper 线程。"""

    def test_request_then_take_returns_the_fence_once(self):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({}))

        svc.request_residual_flush(cq, fence_ts=1700.5)

        assert svc._take_pending_flush(cq) == 1700.5
        assert svc._take_pending_flush(cq) is None, "一次性：取走即消费"

    def test_identity_fence_drops_the_previous_generation_request(self):
        """同一 (task, step) 换代后，上一代挂起的请求不能落到新一代的 CQ 上。"""
        old = FakeCQ(1, 2, name="A")
        new = FakeCQ(1, 2, name="B")
        svc = _service(FakeClients({}))

        svc.request_residual_flush(old, fence_ts=1700.5)

        assert svc._take_pending_flush(new) is None
        assert svc._take_pending_flush(old) is None, "身份不符的条目一并丢弃，不留给旧 CQ"

    def test_cq_without_partition_key_is_skipped(self):
        svc = _service(FakeClients({}))
        svc.request_residual_flush(FakeCQ(1, None), fence_ts=1700.5)
        assert svc._pending_flush == {}

    def test_forget_task_reclaims_unconsumed_requests(self, fake_hls):
        """没被 sweeper 消费的请求随代次记录一起回收，否则它连着 cq 引用一直留着。"""
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({}))            # 注册表已空 = 这一代真的走了
        svc.request_residual_flush(cq, fence_ts=1700.5)

        svc.forget_task(1)

        assert svc._pending_flush == {}

    def test_forget_task_keeps_the_current_generation_request(self, fake_hls):
        """`forget_task` 是**异步**的（只往队列里排任务，前面可能堆着几秒的编码/转码）。

        这期间同一个 task 完全可能已经起了新一代、并为它登记了同键的请求。无差别清就会
        把新一代的请求一起吞掉 —— 那次断流的残帧不被切出来，**长回横跨 gap 的慢放段，
        且静默**。`_claimed_by` 能无差别清是因为它自愈（下一代首写会重新认领），挂起请求
        是一次性的、不自愈，两者不能套同一个论证。
        """
        old = FakeCQ(1, 2, name="A")
        new = FakeCQ(1, 2, name="B")
        svc = _service(FakeClients({1: new}))       # 注册表里已经是新一代
        svc.request_residual_flush(old, fence_ts=1700.5)
        svc.request_residual_flush(new, fence_ts=1800.5)

        svc.forget_task(1)

        assert svc._take_pending_flush(new) == 1800.5, "新一代的请求不该被上一代的回收带走"


# ---------------------------------------------------------------------------
# sweeper
# ---------------------------------------------------------------------------


class RecordingSpy:
    """够用的 RecordingService：只记录 `collect_from` 收到了哪些 CQ。

    sweeper 现在只调这一个方法——**取什么、按什么顺序取全在 service**，所以这个替身不必
    （也不该）复刻那套逻辑。取帧与顺序的断言在 `TestCollectFrom`。
    """

    def __init__(self):
        self.collected = []

    def collect_from(self, cq):
        self.collected.append(cq.name)


class TestSweeper:
    """定时器只剩两件事：扫全量活跃 CQ、逐个交给 `collect_from`。"""

    def test_hands_every_active_cq_to_the_service(self):
        a, b = FakeCQ(1, 2, name="A"), FakeCQ(9, 2, name="B")
        spy = RecordingSpy()

        SegmentSweeper(clients=FakeClients({1: a, 9: b}), service=spy)._sweep()

        assert sorted(spy.collected) == ["A", "B"]

    def test_empty_registry_is_a_no_op(self):
        spy = RecordingSpy()

        SegmentSweeper(clients=FakeClients({}), service=spy)._sweep()

        assert spy.collected == []


# ---------------------------------------------------------------------------
# 生命周期（真队列，真线程）
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_stop_drains_what_was_submitted(self, fake_hls):
        """已提交的段代表已经从 CQ 弹出去的帧，停机必须把它们写完再退。"""
        cq = FakeCQ(1, 2)
        svc = RecordingService(
            config=RecordingConfig(sweep_interval_seconds=60.0),
            clients=FakeClients({1: cq}),
        )
        svc.start()
        try:
            assert svc.submit_segment(cq, "raw", _frames()) is True
        finally:
            svc.stop(timeout=5.0)

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 3)]

    def test_submit_after_stop_is_rejected(self, fake_hls):
        svc = RecordingService(
            config=RecordingConfig(sweep_interval_seconds=60.0),
            clients=FakeClients({}),
        )
        svc.start()
        svc.stop(timeout=5.0)

        assert svc.submit_segment(FakeCQ(), "raw", _frames()) is False


# ---------------------------------------------------------------------------
# 端到端（真队列 + 真 storage.hls；缺外部工具时 skip）
# ---------------------------------------------------------------------------


def _external_tools_available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return Path(settings.ffmpeg_path).exists()


@pytest.mark.skipif(not _external_tools_available(), reason="需要 cv2 与项目自带 ffmpeg")
class TestEndToEnd:
    def test_two_segments_land_in_the_hls_domain_dir(self, tmp_storage):
        from app.storage import hls

        cq = FakeCQ(1, 2)
        svc = RecordingService(
            config=RecordingConfig(sweep_interval_seconds=60.0),
            clients=FakeClients({1: cq}),
        )
        big = [factories.make_frame(ts=1700.0 + i / 15.0, shape=(64, 64, 3)) for i in range(15)]
        later = [factories.make_frame(ts=1800.0 + i / 15.0, shape=(64, 64, 3)) for i in range(15)]

        svc.start()
        try:
            svc.submit_segment(cq, "raw", big)
            svc.submit_segment(cq, "raw", later)
        finally:
            svc.stop(timeout=30.0)

        hls_dir = tmp_storage / "1" / "2" / "hls"
        assert sorted(p.name for p in hls_dir.glob("*.mp4")) == [
            "raw_init.mp4",
            "raw_segment_1700000000.mp4",
            "raw_segment_1800000000.mp4",
        ]
        playlist = hls.playlist_path(1, 2, "raw").read_text(encoding="utf-8")
        assert playlist.count("#EXTINF:") == 2
