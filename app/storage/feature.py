"""
features 域 —— `{step}/features/features.jsonl` 的定位、编解码与读写。

    {root}/{task_id}/{step_id}/features/
      features.jsonl   每帧一行，多流对齐的检测特征（online 推理写回，常开）
      facts.jsonl      同域产物，但**本模块暂不认识它**，见下方「facts.jsonl 为什么不在这里」

对外三个成员，货币是 `FrameFeature`：

    append_features(task, step, features)   追加一批（一次 open("a")，包内不攒批）
    read_features(task, step)               回读整段，按 ts 升序
    delete_features(task, step)             删掉这一份产物（supersede 用）

**管**：目录在哪、文件叫什么、一行是什么、坏行怎么办。
**不管**（都在 `inference/feature/store.py`）：批缓冲 / batch_size、owner fence /
`open_fresh`、best-effort 吞异常——本模块照抛 `OSError`，包成什么由调用方定。

**并发：本域不持锁。** `append_features` 自身不是原子的（一批可能拆成多次底层 write，
Windows 的 `mode="a"` 也不保证追加原子），同一 step 的写与 `tasks.delete_step` 必须由调用
侧串行——现状是 `store.py` 的 `self._lock` 保证同一 step 只有一个写者。

**facts.jsonl 不在这里**：它落盘属本域，但 `EventFact` / `SegmentFact` 住在
`app.services.inference.types`，本层不许 import——本模块最多收发 `Dict[str, Any]`，与
features 侧收发 `FrameFeature` 不对称。在货币拍板前（升格进 `app.domain`，或接受 dict
这层缝）它整个留在 `store.py`：半迁一份产物会让路径知识分裂成两处。

依赖上界：`app.domain`（numpy 随 `Detection.mask` 的类型标注进来）+ stdlib。规范见
`docs/kb/DESIGN_STORAGE_LAYER.md`。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from app.domain.detection import Detection, FrameDetections, FrameFeature
from . import _root

logger = logging.getLogger(__name__)

# 本域的域名 —— 全文件只出现这一次。
# ⚠ 模块叫 `feature`、域目录叫 `features`，刻意不同名；写成 "feature" 会被白名单拦下。
_DOMAIN = "features"

# 产物文件名 —— 内容归各域自己持有，`_root` 对其零知识。
_FEATURES_NAME = "features.jsonl"


# 本域在该 step 下的根目录 —— 域内所有路径都经它
def _domain_root(task_id: int, step_id: int, *, create: bool = False) -> Path:
    return _root.path(task_id, step_id, _DOMAIN, create=create)


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


# ── JSONL 行框定 ─────────────────────────────────────────────────────────────────
#
# 错误语义：**单行坏了跳过 + warning，IO 失败 OSError 原样抛**（包成什么由调用方定）。


def _encode(records: Sequence[Mapping[str, Any]]) -> str:
    """一批 record → 待写文本。**整批先编码完再碰盘**，中途失败时盘上不留半行。"""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


def _decode(path: Path) -> List[Dict[str, Any]]:
    """读整个 JSONL → record 列表。文件不存在返回 `[]`。

    `utf-8-sig` 容忍 Windows 手写文件的 UTF-8 BOM。
    """
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError as e:  # json.JSONDecodeError 是它的子类
                logger.warning("[storage.feature] 跳过损坏行 %s: %s", path, e)
                continue
            # 合法 JSON 但不是对象（`123` / `[1,2]` 都能解析成功）同样算坏行：本域每行
            # 按契约是一条 record，放行会让 `.get` 在下游炸成 AttributeError。
            if not isinstance(rec, dict):
                logger.warning("[storage.feature] 跳过非对象行 %s: %r", path, rec)
                continue
            records.append(rec)
    return records


# ── features.jsonl：对外三个成员 ──────────────────────────────────────────────────


def append_features(task_id: int, step_id: int, features: Sequence[FrameFeature]) -> None:
    """追加一批帧特征：一次 `open("a")` + 一次 write，包内不攒批。

    空序列是 no-op 且**不建目录**（否则 `tasks.list_task_ids()` 会列出一个从没写过东西的 step）。
    编码早于 `mkdir`，失败时盘上不留任何痕迹。

    Raises:
        OSError: 建目录或写文件失败。是否吞掉由调用方定。
    """
    if not features:
        return
    payload = _encode([_feature_to_record(f) for f in features])
    path = _domain_root(task_id, step_id, create=True) / _FEATURES_NAME
    with path.open("a", encoding="utf-8") as f:
        f.write(payload)


def read_features(task_id: int, step_id: int) -> List[FrameFeature]:
    """回读整段特征，**按 ts 升序**（升序是返回值的契约，离线的 `bisect` / 滑窗建立在它上
    面）。文件不存在返回 `[]`；形状不对的 record 与坏行同等对待，跳过 + warning。
    """
    path = _domain_root(task_id, step_id) / _FEATURES_NAME
    frames: List[FrameFeature] = []
    for rec in _decode(path):
        try:
            frames.append(_record_to_feature(rec))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.warning("[storage.feature] 跳过形状不对的 record %s: %s", path, e)
    frames.sort(key=lambda ff: ff.ts)
    return frames


def delete_features(task_id: int, step_id: int) -> bool:
    """删掉 features.jsonl；返回它此前是否存在。

    给写侧的 supersede 用（同 (task, step) 重启 run 前清掉旧序列）。**只执行，不判断该不该
    清**——那是 run 生命周期，归 `inference` 的 `open_fresh`。只删自己这一份产物：同域的
    facts.jsonl 与域目录本身都不碰。
    """
    try:
        (_domain_root(task_id, step_id) / _FEATURES_NAME).unlink()
        return True
    except FileNotFoundError:
        return False
