"""FeatureStore 归属校验：分区键 (task_id, step_id) 跨 restart-supersede 共享，
迟到写（握旧 owner）在分区被新 run open_fresh 接管后必须被拒，防跨 run 串台。

owner 用对象引用（run 身份 = cq 对象），owner 不符即"迟到于 supersede"的确定性代理，
无需造真实竞态即可覆盖归属校验。
"""

import tempfile

from factories import make_frame_detections, make_frame_feature
from app.services.inference.feature.store import FeatureStore


def _result(ts: float, cls: str, n: int):
    return make_frame_feature(
        ts=ts,
        by_source={"bubble": make_frame_detections(n=n, class_name=cls, ts=ts)},
    )


def test_owner_match_lands():
    fs = FeatureStore(tempfile.mkdtemp(), batch_size=1)
    a = object()
    fs.open_fresh(7, 1, owner=a)
    fs.append(7, 1, _result(1.0, "bubble", 2), owner=a)
    frames = fs.load(7, 1)
    assert len(frames) == 1 and len(frames[0].by_source["bubble"].detections) == 2


def test_stale_owner_rejected_after_supersede():
    fs = FeatureStore(tempfile.mkdtemp(), batch_size=1)
    a, b = object(), object()

    # run A 起始 + 写一帧
    fs.open_fresh(7, 1, owner=a)
    fs.append(7, 1, _result(1.0, "bubble", 3), owner=a)

    # run B supersede 同分区（截断）
    fs.open_fresh(7, 1, owner=b)

    # A 的迟到写 → 被拒（分区已属 B）
    fs.append(7, 1, _result(2.0, "bubble", 9), owner=a)
    # B 的正常写 → 落
    fs.append(7, 1, _result(3.0, "bubble", 1), owner=b)

    frames = fs.load(7, 1)
    # 只见 B 的序列：A 起始帧被 open_fresh 截断、A 迟到帧被归属校验拒
    assert len(frames) == 1
    assert frames[0].ts == 3.0
    assert len(frames[0].by_source["bubble"].detections) == 1


def test_owner_none_backward_compatible():
    """不调 open_fresh 时 _owner 恒空 → owner=None 放行（既有直连测试语义不变）。"""
    fs = FeatureStore(tempfile.mkdtemp(), batch_size=1)
    fs.append(7, 1, _result(1.0, "bubble", 2))  # owner 默认 None
    assert len(fs.load(7, 1)) == 1


def test_close_clears_owner_by_identity():
    """close(owner) 身份核对清 owner；被新 run 接管后旧 close 不误清。"""
    fs = FeatureStore(tempfile.mkdtemp(), batch_size=1)
    a, b = object(), object()

    fs.open_fresh(7, 1, owner=a)
    fs.close(7, 1, owner=a)
    assert fs._key(7, 1) not in fs._owner  # 本 run close 清掉自己

    # 新 run 接管后，旧 run 的 close 不应误清 B 的记录
    fs.open_fresh(7, 1, owner=b)
    fs.close(7, 1, owner=a)  # 迟到的 A close
    assert fs._owner.get(fs._key(7, 1)) is b
