"""ClientManager.find_by_source_ip —— 「按点位跟随」查询轴（运行键是 int task_id，source_ip 是被动字段）。

业务不保证 source_ip 唯一：命中多个时按 task_started_at 取**最晚启动**者（新 run 顶掉旧 run 展示）。
"""

from factories import make_cq
from app.services.client.manager import ClientManager


def test_find_by_source_ip_returns_latest_started_and_misses_gracefully():
    cm = ClientManager()
    cq1 = make_cq(task_id=1, step_id=1, source_ip="10.0.0.9")
    cq2 = make_cq(task_id=2, step_id=1, source_ip="10.0.0.9")
    # 显式打戳（避免同一 time.time() tick 下 tie-break 不确定）：cq2 更晚启动
    cq1.task_started_at = 100.0
    cq2.task_started_at = 200.0
    cm.set(cq1.run.task_id, cq1)
    cm.set(cq2.run.task_id, cq2)

    # 同 source_ip 多命中 → 取最晚启动者（新 run 顶掉旧 run 展示），与插入顺序无关
    assert cm.find_by_source_ip("10.0.0.9") is cq2

    # 查不到 → None（terminate/WS 据此 no-op / 黑屏）
    assert cm.find_by_source_ip("9.9.9.9") is None
