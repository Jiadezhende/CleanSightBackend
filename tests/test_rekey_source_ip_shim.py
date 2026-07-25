"""换键：运行键 = **int task_id**；source_ip 为被动字段 + 「按点位跟随」查询轴。

守两条不变式：
1. 同 source_ip 多 task_id → 各占独立槽位并存（不抢占）；
2. find_by_source_ip 命中多个时按 task_started_at 取**最晚启动**者（新 run 顶掉旧 run 展示；
   业务不保证 source_ip 唯一）。
"""

from factories import make_cq
from app.services.client.manager import ClientManager


def test_task_id_is_the_key_and_source_ip_is_separate():
    cq = make_cq(task_id=7, step_id=3, source_ip="10.0.0.1", stage="3")
    assert cq.task_id == 7             # 路由键即 int task_id
    assert cq.source_ip == "10.0.0.1"  # 被动来源字段，与键解耦
    assert not hasattr(cq, "run_key")   # str 副本已删（消双身份）
    assert not hasattr(cq, "client_id")  # 撒谎字段已删


def test_same_source_ip_two_tasks_coexist():
    cm = ClientManager()
    cq1 = make_cq(task_id=1, step_id=1, source_ip="10.0.0.9")
    cq2 = make_cq(task_id=2, step_id=1, source_ip="10.0.0.9")
    cm.set(cq1.task_id, cq1)  # 槽位 1（int 键）
    cm.set(cq2.task_id, cq2)  # 槽位 2

    # 同 source_ip 不抢占：两槽位并存
    assert cm.get(1) is cq1
    assert cm.get(2) is cq2
    assert cm.get_client_count() == 2


def test_find_by_source_ip_returns_latest_started_and_misses_gracefully():
    cm = ClientManager()
    cq1 = make_cq(task_id=1, step_id=1, source_ip="10.0.0.9")
    cq2 = make_cq(task_id=2, step_id=1, source_ip="10.0.0.9")
    # 显式打戳（避免同一 time.time() tick 下 tie-break 不确定）：cq2 更晚启动
    cq1.task_started_at = 100.0
    cq2.task_started_at = 200.0
    cm.set(cq1.task_id, cq1)
    cm.set(cq2.task_id, cq2)

    # 同 source_ip 多命中 → 取最晚启动者（新 run 顶掉旧 run 展示），与插入顺序无关
    assert cm.find_by_source_ip("10.0.0.9") is cq2

    # 查不到 → None（terminate/WS 据此 no-op / 黑屏）
    assert cm.find_by_source_ip("9.9.9.9") is None
