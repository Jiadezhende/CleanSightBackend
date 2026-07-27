"""StageAwareDispatcher 的 peek-commit 轮转排空测试。

覆盖收窄接口（去 capacity、proxy 布尔背压）后的 `_drain_and_submit` 三个不变式：
- 轮转均衡：多 stage 均积压时每 stage 每圈一批交替发，不把额度全给起始 stage（缺口 B 修复）
- 被拒不丢帧：submit 返 False（proxy 限流）时帧原封留 deque，无静默淘汰、无 drop 计数
- 稳态等价：积压 ≤ batch_size 时每 stage 每轮恰好一批（与旧贪婪行为一致）
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.inference.detection.dispatcher import StageAwareDispatcher


def _item(stage: str):
    """deque 里的占位作业：dispatcher 只透传、只 submit 读 batch[0].stage。"""
    return SimpleNamespace(stage=stage)


def _make_dispatcher(submit_batch, *, batch_sizes):
    return StageAwareDispatcher(
        client_manager_instance=MagicMock(),
        active_stages=list(batch_sizes.keys()),
        stage_batch_sizes=batch_sizes,
        submit_batch=submit_batch,
    )


def test_round_robin_balanced_under_backpressure():
    """两 stage 均积压 > batch_size，submit 有限接收：应交替发、不全给起始 stage。"""
    accepted = []           # 记录每个被接收批的 stage
    limit = 3               # 前 3 批接收，之后 proxy 限流

    def fake_submit(batch):
        if len(accepted) >= limit:
            return False    # proxy 满 → 背压
        accepted.append(batch[0].stage)
        return True

    d = _make_dispatcher(fake_submit, batch_sizes={"A": 2, "B": 2})
    for _ in range(10):
        d._stage_queues["A"].append(_item("A"))
        d._stage_queues["B"].append(_item("B"))

    d._drain_and_submit()

    # 轮转起始 offset=0（_round_counter=0）→ 交替 A,B,A：A 得 2 批、B 得 1 批（±1 均衡）
    assert accepted == ["A", "B", "A"]
    assert abs(accepted.count("A") - accepted.count("B")) <= 1
    # 接了才 popleft：A 弹 4 帧、B 弹 2 帧，其余留 deque
    assert len(d._stage_queues["A"]) == 6
    assert len(d._stage_queues["B"]) == 8


def test_rejected_frames_stay_in_deque_no_drop():
    """submit 恒 False（proxy 限流）：帧原封留 deque，无 popleft、无 drop 计数。"""
    d = _make_dispatcher(lambda batch: False, batch_sizes={"A": 4, "B": 4})
    for _ in range(5):
        d._stage_queues["A"].append(_item("A"))
        d._stage_queues["B"].append(_item("B"))

    d._drain_and_submit()

    # 被拒即停、帧不动：深度不变、无静默淘汰计数
    assert len(d._stage_queues["A"]) == 5
    assert len(d._stage_queues["B"]) == 5
    assert d.get_stage_drops().get("A", 0) == 0
    assert d.get_stage_drops().get("B", 0) == 0


def test_steady_state_one_batch_each():
    """积压 ≤ batch_size 时每 stage 每轮恰好一批（稳态贪婪 ≡ 轮转，行为不变）。"""
    calls = []

    def fake_submit(batch):
        calls.append((batch[0].stage, len(batch)))
        return True

    d = _make_dispatcher(fake_submit, batch_sizes={"A": 4, "B": 4})
    for _ in range(2):      # 每 stage 仅 2 帧 < batch_size=4
        d._stage_queues["A"].append(_item("A"))
        d._stage_queues["B"].append(_item("B"))

    d._drain_and_submit()

    # 每 stage 一批、各 2 帧，全部排空
    assert calls == [("A", 2), ("B", 2)]
    assert len(d._stage_queues["A"]) == 0
    assert len(d._stage_queues["B"]) == 0


def test_admit_to_stage_transparent_by_default():
    """背压反馈接缝本次透明：_admit_to_stage 恒 True，取帧不被拦。"""
    d = _make_dispatcher(lambda batch: True, batch_sizes={"A": 2})
    assert d._admit_to_stage("A") is True
    assert d._stage_backpressure == {}
