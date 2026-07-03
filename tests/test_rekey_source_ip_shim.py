"""换键：运行键 = **int task_id**；source_ip 降为被动字段 + 边界垫片（匹配首个）。

守两条不变式：
1. 同 source_ip 多 task_id → 各占独立槽位并存（不抢占）；
2. find_by_source_ip 按 source_ip 匹配首个命中的 run（业务不保证 source_ip 唯一）。
"""

from app.services.client.manager import ClientManager
from app.services.client.queues import ClientQueues


def test_task_id_is_the_key_and_source_ip_is_separate():
    cq = ClientQueues(task_id=7, current_step="3", source_ip="10.0.0.1", stage="3")
    assert cq.task_id == 7             # 路由键即 int task_id
    assert cq.source_ip == "10.0.0.1"  # 被动来源字段，与键解耦
    assert not hasattr(cq, "run_key")   # str 副本已删（消双身份）
    assert not hasattr(cq, "client_id")  # 撒谎字段已删


def test_same_source_ip_two_tasks_coexist():
    cm = ClientManager()
    cq1 = ClientQueues(task_id=1, current_step="1", source_ip="10.0.0.9")
    cq2 = ClientQueues(task_id=2, current_step="1", source_ip="10.0.0.9")
    cm.set(cq1.task_id, cq1)  # 槽位 1（int 键）
    cm.set(cq2.task_id, cq2)  # 槽位 2

    # 同 source_ip 不抢占：两槽位并存
    assert cm.get(1) is cq1
    assert cm.get(2) is cq2
    assert cm.get_client_count() == 2


def test_find_by_source_ip_matches_first_and_misses_gracefully():
    cm = ClientManager()
    cq1 = ClientQueues(task_id=1, current_step="1", source_ip="10.0.0.9")
    cq2 = ClientQueues(task_id=2, current_step="1", source_ip="10.0.0.9")
    cm.set(cq1.task_id, cq1)
    cm.set(cq2.task_id, cq2)

    # 匹配首个命中（顺序取决于 dict，但必是同 source_ip 的某个 run）
    hit = cm.find_by_source_ip("10.0.0.9")
    assert hit in (cq1, cq2)
    assert hit.source_ip == "10.0.0.9"

    # 查不到 → None（terminate/WS 垫片据此 no-op）
    assert cm.find_by_source_ip("9.9.9.9") is None
