"""L1 目标检测产物 —— `{step}/inference/features.jsonl` 的编解码与读写。

    append_features(task, step, features)   追加一批（一次 open("a")，包内不攒批）
    read_features(task, step)               回读整段，按 ts 升序

货币是 `FrameFeature`。**路线 B（追加）**：每帧一条、文件只增不改，没有「先造好一个完整产
物」这回事。删除不在这里——域内删除口径只有 `_layout.delete`（整域）。

**不管**（都在调用方）：批缓冲、run 生命周期、失败要不要吞——本模块照抛 `OSError`。

**并发：本域不持锁。** `append_features` 自身不是原子的（一批可能拆成多次底层 write，
Windows 的 `mode="a"` 也不保证追加原子），同一 step 的写与 `_layout.delete` /
`tasks.delete_step` 必须由调用侧串行。

依赖上界：`app.domain`（numpy 随 `Detection.mask` 的类型标注进来）+ stdlib。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence

from app.domain.detection import Detection, FrameDetections, FrameFeature
from app.storage.inference import _jsonl, _layout

logger = logging.getLogger(__name__)


# ── FrameFeature ↔ 磁盘 record 的对称映射（一对逆运算紧挨放置）────────────────────────
#
# 契约：磁盘 record 是 FrameFeature 的**精简投影** = ts + 每源检测框 (bbox/conf/cls) +
# 帧分辨率。mask / keypoints / metadata 刻意不落，回读按默认还原——投影有损是有意的，故
# 往返断言只在投影后的字段上闭合。磁盘键全命名，无位置约定。


def _serialize_detection(det: Detection) -> Dict[str, Any]:
    """单个 Detection → 特征 dict（bbox 即特征；mask/keypoints 太重不落）。"""
    return {
        "bbox": [int(x) for x in det.bbox],  # 强制原生 int（json 不吃 np.int64），与下方同风格
        "conf": float(det.confidence),
        "cls_id": int(det.class_id),
        "cls": det.class_name,
    }


def _deserialize_detection(d: Mapping[str, Any]) -> Detection:
    """特征 dict → Detection（mask/keypoints 未落盘，回读为 None）。"""
    return Detection(
        bbox=d["bbox"],
        confidence=d["conf"],
        class_id=d["cls_id"],
        class_name=d["cls"],
    )


def _feature_to_record(feature: FrameFeature) -> Dict[str, Any]:
    """FrameFeature → 磁盘 record（逆运算 `_record_to_feature`）。"""
    record: Dict[str, Any] = {
        "ts": feature.ts,
        "features": {
            source: [_serialize_detection(d) for d in fd.detections]
            for source, fd in feature.by_source.items()
        },
    }
    if feature.frame_width is not None and feature.frame_height is not None:
        record["frame_width"] = feature.frame_width
        record["frame_height"] = feature.frame_height
    return record


def _record_to_feature(rec: Mapping[str, Any]) -> FrameFeature:
    """磁盘 record → FrameFeature（`_feature_to_record` 的逆；未落字段按契约默认还原）。

    每源 `FrameDetections.timestamp = 记录级 ts`（同帧多流同源同值）；`metadata={}`、
    `success=True`、`mask/keypoints=None` 均为默认。含 detections 为空的 source ——
    "这一帧该流没检出" 与 "这一帧没有该流" 是两回事，present-key 语义必须保住。
    """
    ts = float(rec.get("ts", 0.0))  # 反序列化边界统一 float（手写 JSONL 可能给 int）
    features = rec.get("features") or {}
    by_source = {
        source: FrameDetections(
            detections=[_deserialize_detection(d) for d in dets],
            metadata={},
            timestamp=ts,
        )
        for source, dets in features.items()
    }
    fw = rec.get("frame_width")
    fh = rec.get("frame_height")
    return FrameFeature(
        ts=ts,
        by_source=by_source,
        frame_width=int(fw) if fw is not None else None,
        frame_height=int(fh) if fh is not None else None,
    )


# ── 对外两个成员 ─────────────────────────────────────────────────────────────────


def append_features(task_id: int, step_id: int, features: Sequence[FrameFeature]) -> None:
    """追加一批帧特征：一次 `open("a")` + 一次 write，包内不攒批（W5）。

    空序列是 no-op 且**不建目录**（否则 `tasks.list_task_ids()` 会列出一个从没写过东西的 step）。
    编码早于 `mkdir`，失败时盘上不留任何痕迹。

    Raises:
        OSError: 建目录或写文件失败。是否吞掉由调用方定。
    """
    if not features:
        return
    payload = _jsonl.encode([_feature_to_record(f) for f in features])
    path = _layout.domain_dir(task_id, step_id, create=True) / _layout.FEATURES_NAME
    with path.open("a", encoding="utf-8") as f:
        f.write(payload)


def read_features(task_id: int, step_id: int) -> List[FrameFeature]:
    """回读整段特征，**按 ts 升序**（升序是返回值的契约，离线的 `bisect` / 滑窗建立在它上
    面）。文件不存在返回 `[]`；形状不对的 record 与坏行同等对待，跳过 + warning。
    """
    path = _layout.domain_dir(task_id, step_id) / _layout.FEATURES_NAME
    frames: List[FrameFeature] = []
    for rec in _jsonl.decode(path):
        try:
            frames.append(_record_to_feature(rec))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.warning("[storage.inference] 跳过形状不对的 record %s: %s", path, e)
    frames.sort(key=lambda ff: ff.ts)
    return frames
