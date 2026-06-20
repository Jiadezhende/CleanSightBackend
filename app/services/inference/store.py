"""特征落盘 + 事实账本（per-task JSONL 追加）。

L2 特征聚合层「隐式」落盘点 —— 实时与离线链路都消费特征：
- FeatureStore：每帧多模型 bbox 特征追加 `{base_dir}/{task_id}/{task_id}.features.jsonl`。
  常开（离线链路硬需求，非可选）。缓冲批量写、best-effort（IO 异常只记日志不抛）。
- FactLedger：L3 产出的事实（EventFact / SegmentFact）追加
  `{base_dir}/{task_id}/{task_id}.facts.jsonl`；`load()` 供离线链路回读。

落盘目录与 HLS 同款 task-dir（`InferenceManager._db_dir` → base_dir）。
两者均为 manager 持有的单例：set_task 时 configure(base_dir)，remove_client 时 close(task_id)。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np

from app.services.inference.data_models import (
    Detection,
    DetectionOutput,
    EventFact,
    SegmentFact,
    fact_from_json,
)

logger = logging.getLogger(__name__)


def _json_safe(obj: Any) -> Any:
    """把 numpy 标量 / 数组转成 JSON 可序列化的原生类型。"""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    return obj


def _serialize_detection(det) -> Dict[str, Any]:
    """单个 Detection → 特征 dict（bbox 即特征；mask/keypoints 太重不落）。"""
    return {
        "bbox": _json_safe(det.bbox),
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


class _JsonlBuffer:
    """per-task 缓冲批量写 JSONL 的共用底座（线程安全、best-effort）。"""

    def __init__(self, base_dir: Union[str, Path], suffix: str, batch_size: int = 64):
        self._base_dir = Path(base_dir)
        self._suffix = suffix          # 如 "features" / "facts"
        self._batch_size = max(1, int(batch_size))
        self._buffers: Dict[str, List[str]] = {}
        self._lock = threading.Lock()

    def configure(self, base_dir: Union[str, Path]) -> None:
        """更新落盘根目录（manager 在 set_task 时调用，与 HLS base_dir 对齐）。"""
        with self._lock:
            self._base_dir = Path(base_dir)

    def _path(self, task_id: Any) -> Path:
        return self._base_dir / str(task_id) / f"{task_id}.{self._suffix}.jsonl"

    def _enqueue(self, task_id: Any, lines: List[str]) -> None:
        if task_id is None or not lines:
            return
        key = str(task_id)
        with self._lock:
            buf = self._buffers.setdefault(key, [])
            buf.extend(lines)
            if len(buf) >= self._batch_size:
                drained = self._buffers.pop(key)
                path = self._path(task_id)
            else:
                return
        self._write(path, drained)

    def _write(self, path: Path, lines: List[str]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(lines))
        except Exception as e:  # best-effort：落盘失败不影响主链路
            logger.warning("[%s] 落盘失败 %s: %s", type(self).__name__, path, e)

    def flush(self, task_id: Optional[Any] = None) -> None:
        """把缓冲写盘。task_id=None 时 flush 全部。"""
        with self._lock:
            if task_id is None:
                pending = [(k, self._buffers.pop(k)) for k in list(self._buffers.keys())]
            else:
                key = str(task_id)
                lines = self._buffers.pop(key, None)
                pending = [(key, lines)] if lines else []
        for key, lines in pending:
            if lines:
                self._write(self._path(key), lines)

    def close(self, task_id: Any) -> None:
        """任务结束：flush 该 task 缓冲并丢弃 buffer 槽。"""
        self.flush(task_id)


class FeatureStore(_JsonlBuffer):
    """多模型 bbox 特征 per-task 落盘（常开）。"""

    def __init__(self, base_dir: Union[str, Path], batch_size: int = 64):
        super().__init__(base_dir, suffix="features", batch_size=batch_size)

    def append(self, task_id: Any, res) -> None:
        """追加一帧的多模型特征。

        Args:
            task_id: 任务 id（落盘目录键）
            res: InferenceResult（duck-typed：.timestamp + .result[task_name]→DetectionOutput）
        """
        if task_id is None:
            return
        try:
            features = {
                task_name: [_serialize_detection(d) for d in out.detections]
                for task_name, out in res.result.items()
            }
            record = {"ts": res.timestamp, "features": features}
            line = json.dumps(record, ensure_ascii=False) + "\n"
        except Exception as e:  # 序列化失败 best-effort 跳过
            logger.warning("[FeatureStore] 特征序列化失败 task=%s: %s", task_id, e)
            return
        self._enqueue(task_id, [line])

    def load(self, task_id: Any, source: str) -> List[DetectionOutput]:
        """离线回读：把某 source（检测点/模型名）的全序列特征还原为 DetectionOutput 列表。

        供离线链路 TemporalAnalyzer.load() 使用。先 flush 缓冲再读，缺失文件返回空。
        注：mask/keypoints/metadata 未落盘，回读的 DetectionOutput.metadata 为空。

        Args:
            task_id: 任务 id
            source: 检测点名（= Detector/Analyzer.name），对应每帧 features[source]
        """
        self.flush(task_id)
        path = self._path(task_id)
        if not path.exists():
            return []
        outputs: List[DetectionOutput] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    dets = (rec.get("features") or {}).get(source)
                    if dets is None:
                        continue
                    outputs.append(DetectionOutput(
                        detections=[_deserialize_detection(d) for d in dets],
                        metadata={},
                        timestamp=rec.get("ts", 0.0),
                    ))
        except Exception as e:
            logger.warning("[FeatureStore] 回读失败 %s: %s", path, e)
        return outputs


class FactLedger(_JsonlBuffer):
    """L3 事实 per-task 账本（EventFact / SegmentFact）。"""

    def __init__(self, base_dir: Union[str, Path], batch_size: int = 16):
        super().__init__(base_dir, suffix="facts", batch_size=batch_size)

    def append(self, task_id: Any, facts: List[Union[EventFact, SegmentFact]]) -> None:
        """追加一批事实。"""
        if task_id is None or not facts:
            return
        try:
            lines = [json.dumps(f.to_json(), ensure_ascii=False) + "\n" for f in facts]
        except Exception as e:
            logger.warning("[FactLedger] 事实序列化失败 task=%s: %s", task_id, e)
            return
        self._enqueue(task_id, lines)

    def load(self, task_id: Any) -> List[Union[EventFact, SegmentFact]]:
        """离线回读：还原该 task 的全部事实（缺失文件返回空）。"""
        self.flush(task_id)  # 确保缓冲落盘后再读
        path = self._path(task_id)
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
