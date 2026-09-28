"""`app.services.recording`：按 run 打包落盘任务 + 取帧顺序 + 断流 flush 的登记与回收。

格式怎么落盘不在这里测（那是 `test_storage_hls.py`）。这里只钉**编排**：

1. **按 run 落盘**——每批产物写进提交时 `cq.run` 指向的 run。同 step 重启后旧一代迟到的段
   写进它自己的 run，不丢、不串进新 run。
2. **打包与拒收**——未绑定 run、空批、队列未起 / 满，直接拒收不入队。
3. **取帧顺序与断流 flush**——整段先于残段；挂起请求一次性、按身份核对，拆除时回收。

主体用例把队列换成同步执行的替身、把 `hls` 换成记录调用的替身，因此不碰线程、不碰
cv2/ffmpeg，全部毫秒级。末尾一条端到端跑真队列 + 真 `storage.hls`，缺外部工具时 skip。
"""

from pathlib import Path

import pytest

import factories
from app.types.run import RunIdentity
from app.services.recording import service as recording_service_module
from app.services.recording._sweeper import SegmentSweeper
from app.services.recording.config import RecordingConfig
from app.services.recording.service import RecordingService
from app.settings import settings


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class FakeCQ:
    """够用的 CQ：`run` 身份 + 取段 / drain 残帧。`bound=False` 造未绑定 run 的 CQ。"""

    def __init__(self, task_id=1, step_id=2, ca_segment_len=3, name="cq", run_id=1, run=None,
                 bound=True):
        if run is None and bound:
            run = RunIdentity(task_id, step_id, run_id)
        self.run = run
        self.ca_segment_len = ca_segment_len
        self.name = name
        self.raw_segments = []          # take_raw_segment 依次弹出
        self.processed_segments = []
        self.raw_residual = []          # drain_ca_raw 一次性取走
        self.processed_residual = []
        self.detections = []              # drain_ca_detections 一次性取走

    def __repr__(self):                 # 让失败信息可读
        return f"<FakeCQ {self.name} run={self.run}>"

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

    def drain_ca_detections(self):
        out, self.detections = list(self.detections), []
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
    """CQ 注册表（sweeper 从它取活跃 CQ）。"""

    def __init__(self, registry=None):
        self.registry = dict(registry or {})

    def get(self, task_id):
        return self.registry.get(task_id)

    def snapshot(self):
        return dict(self.registry)


class FakeHls:
    """记录 `insert_segment` 的调用序与写到了哪个 run。"""

    def __init__(self):
        self.calls = []
        self.runs = []

    def ts_to_us(self, ts):             # `_SegmentJob.label` 用得到
        return int(ts * 1e6)

    def insert_segment(self, run, track, frames):
        self.calls.append(("insert", run.task_id, run.step_id, track, len(frames)))
        self.runs.append(run)

    @property
    def inserts(self):
        return [c for c in self.calls if c[0] == "insert"]


class FakeInference:
    """记录 `append_detections` 的调用序与写到了哪个 run（推理域的落盘替身）。"""

    def __init__(self):
        self.calls = []
        self.runs = []

    def append_detections(self, run, detections):
        self.calls.append(("append", run.task_id, run.step_id, len(detections)))
        self.runs.append(run)

    @property
    def appends(self):
        return [c for c in self.calls if c[0] == "append"]


class InlineQueue:
    """同步执行的队列替身：`submit` 当场把任务跑完，测试不必等线程。

    提交序即执行序这一点与真队列一致（都是 FIFO 单线程语义），所以用它测取帧顺序是
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


@pytest.fixture
def fake_inference(monkeypatch):
    stub = FakeInference()
    monkeypatch.setattr(recording_service_module, "inference", stub)
    return stub


@pytest.fixture
def live_service(fast_task_queue):
    """真队列、真线程的服务构造器（sweeper 间隔拉长，不自己跑）。队列轮询已调快，`stop()` 不白等。"""
    def build(clients) -> RecordingService:
        return RecordingService(
            config=RecordingConfig(sweep_interval_seconds=60.0), clients=clients
        )
    return build


def _service(clients, *, accept=True) -> RecordingService:
    """一个装好两条同步队列的服务（不起线程）。"""
    svc = RecordingService(config=RecordingConfig(), clients=clients)
    svc._hls_queue = InlineQueue(accept=accept)
    svc._detection_queue = InlineQueue(accept=accept)
    return svc


def _frames(n=3, start=1700.0):
    return [factories.make_frame(ts=start + i / 15.0) for i in range(n)]


# ---------------------------------------------------------------------------
# 按 run 落盘：每批产物写进提交时 cq.run 指向的 run
# ---------------------------------------------------------------------------


class TestRunIsolation:
    def test_segment_goes_to_the_cq_run(self, fake_hls):
        cq = FakeCQ(1, 2, run_id=5)
        svc = _service(FakeClients({1: cq}))

        assert svc.submit_segment(cq, "raw", _frames()) is True

        assert fake_hls.runs == [cq.run]
        assert svc._hls_queue.labels == ["seg:1/2/5/raw@1700000000"]

    def test_late_segment_of_the_old_generation_lands_in_its_own_run(self, fake_hls):
        """同 step 重启后，旧一代迟到的段写进旧 run（补全旧录像结尾），不丢、不写进新 run。"""
        old = FakeCQ(1, 2, run_id=5, name="A")
        new = FakeCQ(1, 2, run_id=6, name="B")
        svc = _service(FakeClients({1: new}))

        svc.submit_segment(old, "raw", _frames())
        svc.submit_segment(new, "raw", _frames(start=1800.0))

        assert fake_hls.runs == [old.run, new.run]

    def test_unregistered_cq_still_writes(self, fake_hls):
        """拆除后（CQ 已出注册表）才执行的残段照常写进自己的 run。"""
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({}))

        svc.submit_segment(cq, "raw", _frames())

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 3)]


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
        assert svc._hls_queue.labels == []

    def test_cq_without_run(self, fake_hls):
        svc = _service(FakeClients({}))
        cq = FakeCQ(bound=False)
        assert svc.submit_segment(cq, "raw", _frames()) is False
        assert svc._hls_queue.labels == []

    def test_full_queue_returns_false_and_writes_nothing(self, fake_hls):
        svc = _service(FakeClients({}), accept=False)
        assert svc.submit_segment(FakeCQ(), "raw", _frames()) is False
        assert fake_hls.calls == []


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
        assert svc._hls_queue.labels == []

    def test_cq_without_run_is_skipped(self, fake_hls):
        cq = FakeCQ(bound=False)
        cq.raw_residual = _frames(3)
        svc = _service(FakeClients({}))

        svc.flush_residual(cq)

        assert svc._hls_queue.labels == []

    def test_residual_carries_its_own_run(self, fake_hls):
        """残段写进**自己那一代**的 run：拆除后注册表已换人也不会串台。"""
        old = FakeCQ(1, 2, ca_segment_len=2, name="A", run_id=5)
        old.raw_residual = _frames(2)
        svc = _service(FakeClients({1: FakeCQ(1, 2, name="B", run_id=6)}))

        svc.flush_residual(old)

        assert fake_hls.runs == [old.run]

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

    def test_cq_without_run_keeps_its_frames_buffered(self, fake_hls):
        """定位不到落盘目录就**不取帧** —— 取了只能丢。"""
        cq = FakeCQ(bound=False)
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

    def test_cq_without_run_is_skipped(self):
        svc = _service(FakeClients({}))
        svc.request_residual_flush(FakeCQ(bound=False), fence_ts=1700.5)
        assert svc._pending_flush == {}

    def test_teardown_flush_reclaims_the_unconsumed_request(self, fake_hls):
        """没被 sweeper 消费的请求在拆除时回收，否则它连着 cq 引用一直留着。"""
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({}))
        svc.request_residual_flush(cq, fence_ts=1700.5)

        svc.flush_residual(cq)              # 拆除路径（until_ts=None）

        assert svc._pending_flush == {}

    def test_teardown_flush_keeps_another_generation_request(self, fake_hls):
        """按身份回收：拆旧 CQ 不能把新一代同键的请求一起吞掉（那次断流的残帧就切不出来了）。"""
        old = FakeCQ(1, 2, name="A", run_id=5)
        new = FakeCQ(1, 2, name="B", run_id=6)
        svc = _service(FakeClients({1: new}))
        svc.request_residual_flush(new, fence_ts=1800.5)

        svc.flush_residual(old)

        assert svc._take_pending_flush(new) == 1800.5

    def test_fenced_flush_does_not_reclaim(self, fake_hls):
        """断流期的 flush（给了 until_ts）不是拆除，不回收挂起请求。"""
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({}))
        svc.request_residual_flush(cq, fence_ts=1700.5)

        svc.flush_residual(cq, until_ts=1700.5)

        assert svc._take_pending_flush(cq) == 1700.5


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
# 检测结果落盘：另一条队列，其余与段写同构
# ---------------------------------------------------------------------------


def _dets(n=2, start=1700.0):
    return [factories.make_frame_detection(ts=start + i / 15.0) for i in range(n)]


class TestSubmitDetections:
    def test_lands_through_its_own_queue(self, fake_inference):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))

        assert svc.submit_detections(cq, _dets()) is True
        assert fake_inference.appends == [("append", 1, 2, 2)]
        assert svc._detection_queue.labels == ["det:1/2/1×2"]

    def test_does_not_touch_the_hls_queue(self, fake_hls, fake_inference):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))

        svc.submit_detections(cq, _dets())

        assert svc._hls_queue.labels == []
        assert fake_hls.calls == []

    def test_queue_not_started(self, fake_inference):
        svc = RecordingService(config=RecordingConfig(), clients=FakeClients({}))
        assert svc.submit_detections(FakeCQ(), _dets()) is False
        assert fake_inference.calls == []

    def test_empty_batch_never_reaches_the_queue(self, fake_inference):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))
        assert svc.submit_detections(cq, []) is False
        assert svc._detection_queue.labels == []

    def test_cq_without_run(self, fake_inference):
        cq = FakeCQ(bound=False)
        svc = _service(FakeClients({}))
        assert svc.submit_detections(cq, _dets()) is False
        assert svc._detection_queue.labels == []


class TestDetectionRunIsolation:
    def test_late_batch_of_the_old_generation_lands_in_its_own_run(self, fake_inference):
        old = FakeCQ(1, 2, name="A", run_id=5)
        new = FakeCQ(1, 2, name="B", run_id=6)
        svc = _service(FakeClients({1: new}))

        svc.submit_detections(old, _dets())

        assert fake_inference.runs == [old.run]


class TestCollectAndFlushDetections:
    def test_collect_pulls_the_detection_buffer(self, fake_hls, fake_inference):
        cq = FakeCQ(1, 2)
        cq.detections = _dets(n=3)
        svc = _service(FakeClients({1: cq}))

        svc.collect_from(cq)

        assert fake_inference.appends == [("append", 1, 2, 3)]
        assert cq.detections == []                      # 取走即清

    def test_collect_with_empty_buffer_submits_nothing(self, fake_hls, fake_inference):
        cq = FakeCQ(1, 2)
        svc = _service(FakeClients({1: cq}))

        svc.collect_from(cq)

        assert svc._detection_queue.labels == []

    def test_teardown_flush_hands_over_the_tail(self, fake_hls, fake_inference):
        """拆除期 flush_residual 必须把缓冲里剩下的交出去，否则每个 step 尾部稳定少一截。"""
        cq = FakeCQ(1, 2)
        cq.detections = _dets(n=2)
        svc = _service(FakeClients({1: cq}))

        svc.flush_residual(cq)

        assert fake_inference.appends == [("append", 1, 2, 2)]

    def test_pending_flush_path_drains_detections_once(self, fake_hls, fake_inference):
        """断流那条路：collect_from 走 flush_residual 分支，检测结果只被交出一次。"""
        cq = FakeCQ(1, 2)
        cq.detections = _dets(n=2)
        svc = _service(FakeClients({1: cq}))
        svc.request_residual_flush(cq, fence_ts=1700.5)

        svc.collect_from(cq)

        assert fake_inference.appends == [("append", 1, 2, 2)]


# ---------------------------------------------------------------------------
# 生命周期（真队列，真线程）
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_stop_drains_what_was_submitted(self, fake_hls, live_service):
        """已提交的段代表已经从 CQ 弹出去的帧，停机必须把它们写完再退。"""
        cq = FakeCQ(1, 2)
        svc = live_service(FakeClients({1: cq}))
        svc.start()
        try:
            assert svc.submit_segment(cq, "raw", _frames()) is True
        finally:
            svc.stop(timeout=5.0)

        assert fake_hls.inserts == [("insert", 1, 2, "raw", 3)]


class TestDetectionEndToEnd:
    """真队列 + 真 `storage.inference`（纯 stdlib，不需要外部工具）。"""

    def test_detections_land_in_the_inference_domain_dir(self, tmp_storage, live_service):
        from app.storage import inference, runs

        cq = FakeCQ(run=runs.allocate(1, 2))
        svc = live_service(FakeClients({1: cq}))
        svc.start()
        try:
            assert svc.submit_detections(cq, _dets(n=3, start=1700.0)) is True
        finally:
            svc.stop(timeout=5.0)

        run_dir = tmp_storage / "1" / "2" / str(cq.run.run_id)
        assert (run_dir / "inference" / "detections.jsonl").exists()
        assert [round(ff.ts, 4) for ff in inference.read_detections(cq.run)] == [
            round(1700.0 + i / 15.0, 4) for i in range(3)
        ]


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
    def test_two_segments_land_in_the_hls_domain_dir(self, tmp_storage, live_service):
        from app.storage import hls, runs

        cq = FakeCQ(run=runs.allocate(1, 2))
        svc = live_service(FakeClients({1: cq}))
        big = [factories.make_frame(ts=1700.0 + i / 15.0, shape=(64, 64, 3)) for i in range(15)]
        later = [factories.make_frame(ts=1800.0 + i / 15.0, shape=(64, 64, 3)) for i in range(15)]

        svc.start()
        try:
            svc.submit_segment(cq, "raw", big)
            svc.submit_segment(cq, "raw", later)
        finally:
            svc.stop(timeout=30.0)

        hls_dir = tmp_storage / "1" / "2" / str(cq.run.run_id) / "hls"
        assert sorted(p.name for p in hls_dir.glob("*.mp4")) == [
            "raw_init.mp4",
            "raw_segment_1700000000.mp4",
            "raw_segment_1800000000.mp4",
        ]
        playlist = hls.playlist_path(cq.run, "raw").read_text(encoding="utf-8")
        assert playlist.count("#EXTINF:") == 2
