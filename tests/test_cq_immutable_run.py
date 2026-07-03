"""T1: CQ per-run 不可变 —— 身份构造注入、复用机器已删、换槽、存储 supersede。"""

from app.domain.detection import FrameDetections
from app.services.client.manager import ClientManager
from app.services.client.queues import ClientQueues
from app.services.inference.feature.store import FeatureStore


def test_cq_identity_immutable_and_reuse_machinery_gone():
    cq = ClientQueues(task_id=7, current_step="3", source_ip="ip1", stage="3")
    assert cq.get_task_id() == 7
    assert cq.get_step_id() == 3
    assert cq.get_stage() == "3"
    assert cq.source_ip == "ip1"
    assert cq.run_key == "7"

    # 复用机器已删除（一 CQ == 一 run，不原地改身份）
    for gone in ("set_task", "set_stage", "clear_task_caches", "get_task", "client_id"):
        assert not hasattr(cq, gone), f"{gone} 应已删除"

    # clear() 只释放 payload，不重置不可变身份
    cq.push_detection("x", FrameDetections(detections=[], metadata={}, timestamp=1.0))
    cq.clear()
    assert cq.get_task_id() == 7
    assert cq.get_step_id() == 3
    assert cq.get_stage() == "3"


def test_step_id_none_for_unparseable_step():
    cq = ClientQueues(task_id=1, current_step="测漏", source_ip="ip")
    assert cq.get_step_id() is None
    assert cq.get_task_id() == 1


def test_bare_cq_has_no_identity():
    cq = ClientQueues()  # 纯队列单测形态（无身份）
    assert cq.get_task_id() is None
    assert cq.get_step_id() is None
    assert cq.get_stage() == "MOCK"
    assert cq.run_key == ""


def test_client_manager_set_replaces_slot_with_new_object():
    cm = ClientManager()
    cq1 = ClientQueues(task_id=1, current_step="1", source_ip="c")
    cm.set("1", cq1)
    assert cm.get("1") is cq1

    # 换 run = 建**新** CQ 换槽（不在旧对象上改）
    cq2 = ClientQueues(task_id=1, current_step="1", source_ip="c")
    cm.set("1", cq2)
    assert cm.get("1") is cq2
    assert cm.get("1") is not cq1


def test_open_fresh_supersedes_storage_partition(tmp_path):
    fs = FeatureStore(tmp_path)
    p = fs._path(1, 2)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("old-run-line\n")
    assert p.exists()

    # 新 run 起始截断该 (task, step) 分区 → 无新旧混写
    fs.open_fresh(1, 2)
    assert not p.exists()
