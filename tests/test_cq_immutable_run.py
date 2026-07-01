"""T1: CQ per-run 不可变 —— 身份构造注入、复用机器已删、换槽、存储 supersede。"""

from types import SimpleNamespace

from app.domain.detection import FrameDetections
from app.services.client.manager import ClientManager
from app.services.client.queues import ClientQueues
from app.services.inference.feature.store import FeatureStore


def _task(task_id: int, step: str):
    return SimpleNamespace(task_id=task_id, current_step=step, status="running")


def test_cq_identity_immutable_and_reuse_machinery_gone():
    cq = ClientQueues("ip1", task=_task(7, "3"), stage="3")
    assert cq.get_task_id() == 7
    assert cq.get_step_id() == 3
    assert cq.get_stage() == "3"
    assert cq.source_ip == "ip1"

    # 复用机器已删除（一 CQ == 一 run，不原地改身份）
    for gone in ("set_task", "set_stage", "clear_task_caches"):
        assert not hasattr(cq, gone), f"{gone} 应已删除"

    # clear() 只释放 payload，不重置不可变身份
    cq.push_detection("x", FrameDetections(detections=[], metadata={}, timestamp=1.0))
    cq.clear()
    assert cq.get_task_id() == 7
    assert cq.get_step_id() == 3
    assert cq.get_stage() == "3"


def test_step_id_none_for_unparseable_step():
    cq = ClientQueues("ip", task=_task(1, "测漏"))
    assert cq.get_step_id() is None
    assert cq.get_task_id() == 1


def test_bare_cq_has_no_identity():
    cq = ClientQueues(client_id="c")  # task=None（纯队列单测形态）
    assert cq.get_task() is None
    assert cq.get_task_id() is None
    assert cq.get_step_id() is None
    assert cq.get_stage() == "MOCK"


def test_client_manager_set_replaces_slot_with_new_object():
    cm = ClientManager()
    cq1 = ClientQueues("c", task=_task(1, "1"))
    cm.set("c", cq1)
    assert cm.get("c") is cq1

    # 换 task = 建**新** CQ 换槽（不在旧对象上改）
    cq2 = ClientQueues("c", task=_task(2, "1"))
    cm.set("c", cq2)
    assert cm.get("c") is cq2
    assert cm.get("c") is not cq1
    assert cm.get("c").get_task_id() == 2


def test_open_fresh_supersedes_storage_partition(tmp_path):
    fs = FeatureStore(tmp_path)
    p = fs._path(1, 2)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("old-run-line\n")
    assert p.exists()

    # 新 run 起始截断该 (task, step) 分区 → 无新旧混写
    fs.open_fresh(1, 2)
    assert not p.exists()
