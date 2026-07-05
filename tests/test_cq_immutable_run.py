"""T1: CQ per-run 不可变 —— 身份构造注入、复用机器已删、换槽、存储 supersede。"""

from factories import make_bare_cq, make_cq, make_frame_detections
from app.services.client.manager import ClientManager
from app.services.inference.feature.store import FeatureStore


def test_cq_identity_immutable_and_reuse_machinery_gone():
    cq = make_cq(task_id=7, step_id=3, source_ip="ip1", stage="3")
    assert cq.task_id == 7
    assert cq.step_id == 3
    assert cq.stage == "3"
    assert cq.source_ip == "ip1"

    # 复用机器 + str 副本 + 撒谎字段均已删除（一 CQ == 一 run，键即 int task_id）
    for gone in ("set_task", "set_stage", "clear_task_caches", "get_task", "client_id", "run_key"):
        assert not hasattr(cq, gone), f"{gone} 应已删除"

    # clear() 只释放 payload，不重置不可变身份
    cq.push_detection("x", make_frame_detections(n=0, ts=1.0))
    cq.clear()
    assert cq.task_id == 7
    assert cq.step_id == 3
    assert cq.stage == "3"


def test_step_id_stored_verbatim():
    # step_id 已在 RunController 边界解析好，构造直存（本类不再做字符串解析）
    cq = make_cq(task_id=1, step_id=None, source_ip="ip")
    assert cq.step_id is None
    assert cq.task_id == 1


def test_bare_cq_has_no_identity():
    cq = make_bare_cq()  # 纯队列单测形态（无身份）
    assert cq.task_id is None
    assert cq.step_id is None
    assert cq.stage == "MOCK"


def test_client_manager_set_replaces_slot_with_new_object():
    cm = ClientManager()
    cq1 = make_cq(task_id=1, step_id=1, source_ip="c")
    cm.set(1, cq1)               # int 键
    assert cm.get(1) is cq1

    # 换 run = 建**新** CQ 换槽（不在旧对象上改）
    cq2 = make_cq(task_id=1, step_id=1, source_ip="c")
    cm.set(1, cq2)
    assert cm.get(1) is cq2
    assert cm.get(1) is not cq1


def test_open_fresh_supersedes_storage_partition(tmp_path):
    fs = FeatureStore(tmp_path)
    p = fs._path(1, 2)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("old-run-line\n")
    assert p.exists()

    # 新 run 起始截断该 (task, step) 分区 → 无新旧混写
    fs.open_fresh(1, 2)
    assert not p.exists()
