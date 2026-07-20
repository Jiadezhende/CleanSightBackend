"""特征落盘 + 事实账本（per-(task, step) JSONL 追加）。

L2 特征聚合层「隐式」落盘点 —— 实时与离线链路都消费特征：
- FeatureStore：每帧多模型 bbox 特征追加 `{base_dir}/{task_id}/{step_id}/features.jsonl`。
  常开（离线链路硬需求，非可选）。缓冲批量写、best-effort（IO 异常只记日志不抛）。
- FactLedger：L3 产出的事实（EventFact / SegmentFact）追加
  `{base_dir}/{task_id}/{step_id}/facts.jsonl`；`load()` 供离线链路回读（offline 预置）。

落盘目录 `{task_id}/{step_id}/` 与 HLS 同款工作目录（`InferenceManager._db_dir` → base_dir,
见 `hls_strategy._persist_*_segment`），随 step 目录被 cleanup TTL 连带回收。
两者均为 manager 持有的单例：构造时绑定 base_dir，stop_workflow 时 close(task_id, step_id)。

帧对齐契约：每条记录的 `ts` = 该帧的 `FrameFeature.ts`（= 帧捕获 ts，与在线滑窗同源），
与 HLS keypoints/段落盘所用的 `fd.timestamp` 同源同值，故 feature 行可按 `ts` 精确对上
同帧的 HLS 证据片段与 keypoints。`append`/`load` 两端货币均为帧级 `FrameFeature`
（`ts + {source: FrameDetections}`，离线与在线共用一套货币）；磁盘 record 是它的**精简投影**
（只落离线必要信息，mask/keypoints/metadata 不落），对称映射见 `_feature_to_record`/`_record_to_feature`。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.domain.detection import Detection, FrameDetections, FrameFeature
from app.services.inference.models import EventFact, SegmentFact, fact_from_json

logger = logging.getLogger(__name__)


def _serialize_detection(det) -> Dict[str, Any]:
    """单个 Detection → 特征 dict（bbox 即特征；mask/keypoints 太重不落）。"""
    return {
        "bbox": [int(x) for x in det.bbox],  # 强制原生 int（json 不吃 np.int64），与下方 conf/cls_id 同风格
        "conf": float(det.confidence),
        "cls_id": int(det.class_id),
        "cls": det.class_name,
    }


def _deserialize_detection(d: Dict[str, Any]) -> Detection:
    """特征 dict → Detection（mask/keypoints 未落盘，回读为 None）。"""
    return Detection(
        bbox=d["bbox"],
        confidence=d["conf"],
        class_id=d["cls_id"],
        class_name=d["cls"],
    )


# ── FrameFeature ↔ 磁盘 record 的对称映射（store 私有格式，一对逆运算紧挨放置）──────────
#
# 契约：append/load 两端货币都是 FrameFeature；磁盘 record 是它的**精简投影**，只保留离线
# 必要信息 = ts + 每源检测框(bbox/conf/cls) + 帧分辨率。刻意不落（回读按默认还原）：
#   - mask/keypoints：重（seg/pose 才有，每帧一张数组），且离线不消费；
#   - metadata / success / error：离线不消费。
# 磁盘键全命名（frame_width/frame_height），无位置约定；位置约定只活在 Detection 的
# bbox=[x1,y1,x2,y2]（其本身就是坐标序）。


def _feature_to_record(feature: FrameFeature) -> Dict[str, Any]:
    """FrameFeature → 磁盘 record（逆运算 _record_to_feature）。"""
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


def _record_to_feature(rec: Dict[str, Any]) -> FrameFeature:
    """磁盘 record → FrameFeature（_feature_to_record 的逆；未落字段按契约默认还原）。

    每源 `FrameDetections.timestamp = 记录级 ts`（同帧多流同源同值）；`metadata={}`、
    `success=True`、`mask/keypoints=None` 均为默认。含 detections 为空的 source。
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


class _JsonlBuffer:
    """per-(task, step) 缓冲批量写 JSONL 的共用底座（线程安全、best-effort）。

    落盘键为 `(task_id, step_id)`，工作目录 `{base}/{task_id}/{step_id}/`，与 HLS 同款。
    buffer 槽以 `f"{task_id}/{step_id}"` 为 key，value 存 `(task_id, step_id, lines)`，
    使全量 flush 不需从 key 反解路径。
    """

    def __init__(self, base_dir: Union[str, Path], suffix: str, batch_size: int = 64):
        self._base_dir = Path(base_dir)
        self._suffix = suffix          # 如 "features" / "facts"
        self._batch_size = max(1, int(batch_size))
        # key=f"{task_id}/{step_id}" → (task_id, step_id, lines)
        self._buffers: Dict[str, tuple] = {}
        # key → 当前 run 的 owner（= cq 对象引用，run 身份）。open_fresh 设、_enqueue 校：
        # 分区键 (task_id, step_id) 跨 restart-supersede 共享，非 per-run 身份；迟到写握旧 owner
        # 时若分区已被新 run open_fresh 截断，靠此拒掉，防跨 run 串台。set/check 与所有文件
        # 写/unlink 全在 self._lock 下串行 → 无 TOCTOU（check→落盘之间不会被 open_fresh 插入）。
        self._owner: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def configure(self, base_dir: Union[str, Path]) -> None:
        """更新落盘根目录（manager 构造时调用，与 HLS base_dir 对齐）。"""
        with self._lock:
            self._base_dir = Path(base_dir)

    @staticmethod
    def _key(task_id: Any, step_id: Any) -> str:
        return f"{task_id}/{step_id}"

    def _path(self, task_id: Any, step_id: Any) -> Path:
        return self._base_dir / str(task_id) / str(step_id) / f"{self._suffix}.jsonl"

    def _enqueue(
        self, task_id: Any, step_id: Any, lines: List[str], owner: Any = None
    ) -> None:
        """入队并按批落盘。owner = 本次写属的 run（cq 对象）；与当前分区 owner 不符即拒
        （迟到于 supersede）。落盘 `_write` 收进锁内，与 open_fresh 的 unlink 互斥。"""
        if task_id is None or step_id is None or not lines:
            return
        key = self._key(task_id, step_id)
        with self._lock:
            cur = self._owner.get(key)
            if cur is not None and cur is not owner:
                # 分区已被新 run open_fresh 接管：迟到写握旧 owner，拒掉防串台。
                logger.debug(
                    "[%s] 拒迟到写（分区已 supersede）key=%s", type(self).__name__, key
                )
                return
            _, _, buf = self._buffers.setdefault(key, (task_id, step_id, []))
            buf.extend(lines)
            if len(buf) >= self._batch_size:
                _, _, drained = self._buffers.pop(key)
                self._write(self._path(task_id, step_id), drained)

    def _write(self, path: Path, lines: List[str]) -> None:
        """落盘一批行。**仅在持 self._lock 时调用**——与 open_fresh 的 unlink 互斥，
        保证「迟到 drain 写」不会重建已被新 run 截断的文件。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(lines))
        except Exception as e:  # best-effort：落盘失败不影响主链路
            logger.warning("[%s] 落盘失败 %s: %s", type(self).__name__, path, e)

    def flush(self, task_id: Optional[Any] = None, step_id: Optional[Any] = None) -> None:
        """把缓冲写盘。task_id=None 时 flush 全部；否则 flush 指定 (task_id, step_id)。

        全程持锁（含 _write），与 open_fresh 互斥，同 _enqueue 口径。"""
        with self._lock:
            if task_id is None:
                pending = list(self._buffers.values())
                self._buffers.clear()
            else:
                entry = self._buffers.pop(self._key(task_id, step_id), None)
                pending = [entry] if entry else []
            for t_id, s_id, lines in pending:
                if lines:
                    self._write(self._path(t_id, s_id), lines)

    def close(self, task_id: Any, step_id: Any, owner: Any = None) -> None:
        """任务结束：flush 该 (task, step) 缓冲并丢弃 buffer 槽。

        owner 传入时按身份核对清 owner 记录（释放 cq 引用、界定 _owner 增长）；仅当当前
        owner 仍是本 run 才清——防误清已被新 run 接管的记录。与 flush 顺序取锁、不嵌套。
        """
        self.flush(task_id, step_id)
        if owner is not None:
            key = self._key(task_id, step_id)
            with self._lock:
                if self._owner.get(key) is owner:
                    del self._owner[key]

    def open_fresh(self, task_id: Any, step_id: Any, owner: Any = None) -> None:
        """新 run 起始：登记 owner、丢弃缓冲槽并删除已落盘文件（重启 = supersede）。

        追加写模式下，同 (task, step) 重启会新旧混写；一次 run 起始截断该分区，
        保证读到的永远是本 run 的完整、干净序列。owner = 新 run 的 cq 对象，作后续
        _enqueue 的身份基准。登记 + pop + unlink 全在锁内，与 _enqueue 的落盘互斥
        （迟到 drain 写要么在 unlink 前入文件随后被删、要么在 unlink 后被 owner 拒，无中间态）。
        best-effort：删除失败不阻断起流。
        """
        if task_id is None or step_id is None:
            return
        key = self._key(task_id, step_id)
        path = self._path(task_id, step_id)
        with self._lock:
            self._owner[key] = owner
            self._buffers.pop(key, None)
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:  # best-effort：截断失败不影响主链路
                logger.warning(
                    "[%s] open_fresh 截断失败 %s: %s", type(self).__name__, path, e
                )


class FeatureStore(_JsonlBuffer):
    """多模型 bbox 特征 per-task 落盘（常开）。"""

    def __init__(self, base_dir: Union[str, Path], batch_size: int = 64):
        super().__init__(base_dir, suffix="features", batch_size=batch_size)

    def append(self, task_id: Any, step_id: Any, feature: "FrameFeature", owner: Any = None) -> None:
        """追加一帧的多流特征（帧级 FrameFeature）。

        Args:
            task_id: 任务 id（落盘目录键）
            step_id: 洗消步骤 id（落盘目录键，与 HLS 同款 `{task_id}/{step_id}/`）
            feature: FrameFeature（.ts + .by_source[流名]→FrameDetections，与在线滑窗同源同型）
            owner: 本次写属的 run（= cq 对象）；与分区当前 owner 不符即被拒（迟到于 supersede）
        """
        if task_id is None or step_id is None:
            return
        try:
            line = json.dumps(_feature_to_record(feature), ensure_ascii=False) + "\n"
        except Exception as e:  # 序列化失败 best-effort 跳过
            logger.warning("[FeatureStore] 特征序列化失败 task=%s step=%s: %s", task_id, step_id, e)
            return
        self._enqueue(task_id, step_id, [line], owner=owner)

    def load(self, task_id: Any, step_id: Any) -> List[FrameFeature]:
        """离线回读：把整段特征还原为帧级 `FrameFeature` 序列（与在线滑窗同型）。

        先 flush 缓冲，再**单次顺序扫 features.jsonl**，每行经 `_record_to_feature` 还原成一个
        `FrameFeature`（含该行 features 里的全部 source，含 detections 为空的 source，present-key
        天然落在 by_source 键集上）。返回按 `ts` 升序。文件缺失返回 `[]`；单行损坏记 warning 后跳过、
        不中断其余数据。未落字段（mask/keypoints/metadata/success）按契约默认还原，详见 `_record_to_feature`。

        Args:
            task_id: 任务 id
            step_id: 洗消步骤 id
        """
        frames: List[FrameFeature] = []
        self.flush(task_id, step_id)
        path = self._path(task_id, step_id)
        if not path.exists():
            return frames
        try:
            # utf-8-sig 容忍 Windows 手写 features.jsonl 的 UTF-8 BOM；后端自身写出的无 BOM 亦正常。
            with path.open("r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception as e:  # 单行损坏：跳过不中断其余数据
                        logger.warning("[FeatureStore] 跳过损坏行 %s: %s", path, e)
                        continue
                    frames.append(_record_to_feature(rec))
        except Exception as e:
            logger.warning("[FeatureStore] 回读失败 %s: %s", path, e)
        frames.sort(key=lambda ff: ff.ts)
        return frames


class FactLedger(_JsonlBuffer):
    """L3 事实 per-task 账本（EventFact / SegmentFact）。"""

    def __init__(self, base_dir: Union[str, Path], batch_size: int = 16):
        super().__init__(base_dir, suffix="facts", batch_size=batch_size)

    def append(
        self,
        task_id: Any,
        step_id: Any,
        facts: List[Union[EventFact, SegmentFact]],
        owner: Any = None,
    ) -> None:
        """追加一批事实（offline 预置；online 链路不再写）。owner 同 FeatureStore.append。"""
        if task_id is None or step_id is None or not facts:
            return
        try:
            lines = [json.dumps(f.to_json(), ensure_ascii=False) + "\n" for f in facts]
        except Exception as e:
            logger.warning("[FactLedger] 事实序列化失败 task=%s step=%s: %s", task_id, step_id, e)
            return
        self._enqueue(task_id, step_id, lines, owner=owner)

    def load(self, task_id: Any, step_id: Any) -> List[Union[EventFact, SegmentFact]]:
        """离线回读：还原该 (task, step) 的全部事实（缺失文件返回空）。"""
        self.flush(task_id, step_id)  # 确保缓冲落盘后再读
        path = self._path(task_id, step_id)
        if not path.exists():
            return []
        facts: List[Union[EventFact, SegmentFact]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        facts.append(fact_from_json(json.loads(line)))
        except Exception as e:
            logger.warning("[FactLedger] 回读失败 %s: %s", path, e)
        return facts

    def replace_segments(
        self,
        task_id: Any,
        step_id: Any,
        producer: str,
        facts: List[SegmentFact],
    ) -> None:
        """幂等替换某 producer 的分段事实（离线链路专用）。

        语义：先 flush 本 (task,step) 缓冲 → 读既有 facts.jsonl → 保留所有 EventFact 与
        `meta.producer != producer` 的其他 SegmentFact → 删同 producer 旧 SegmentFact →
        追加本次新事实 → 写同目录临时文件并 `os.replace()` 原子替换。写/序列化失败保留旧文件。

        全程持 `self._lock`（与 _write/_enqueue/open_fresh 互斥）串行 read-modify-write；
        不调用 `flush()`（其会重入锁），改在锁内 inline flush 本 key 缓冲。
        一期不支持同一 (task,step) 跨进程并发执行。空 facts 视为清除该 producer 旧分段。

        Args:
            task_id: 任务 id
            step_id: 洗消步骤 id
            producer: 生产者身份（= segmenter.name），写入每条新事实 meta.producer
            facts: 本次产出的 SegmentFact 列表（可空）
        """
        if task_id is None or step_id is None:
            return
        key = self._key(task_id, step_id)
        path = self._path(task_id, step_id)
        with self._lock:
            # 1) inline flush 本 key 缓冲（不重入 flush()）
            entry = self._buffers.pop(key, None)
            if entry:
                _, _, buffered = entry
                if buffered:
                    self._write(path, buffered)
            # 2) 读既有事实（单行损坏跳过，不丢整文件）
            existing: List[Union[EventFact, SegmentFact]] = []
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                existing.append(fact_from_json(json.loads(line)))
                            except Exception as e:
                                logger.warning("[FactLedger] 跳过损坏行 %s: %s", path, e)
                except Exception as e:
                    logger.warning("[FactLedger] replace 读既有失败 %s: %s", path, e)
            # 3) 保留 EventFact + 其他 producer 的 SegmentFact，删同 producer 旧分段
            kept = [
                f for f in existing
                if not (isinstance(f, SegmentFact) and f.meta.get("producer") == producer)
            ]
            merged = kept + list(facts)
            # 4) 原子替换：写临时文件 → os.replace；失败保留旧文件
            tmp = path.with_name(path.name + ".tmp")
            try:
                lines = [json.dumps(f.to_json(), ensure_ascii=False) + "\n" for f in merged]
                path.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("w", encoding="utf-8") as f:
                    f.write("".join(lines))
                os.replace(tmp, path)
            except Exception as e:
                logger.warning("[FactLedger] replace 写失败（保留旧文件）%s: %s", path, e)
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                raise
