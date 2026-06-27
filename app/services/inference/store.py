"""特征落盘 + 事实账本（per-(task, step) JSONL 追加）。

L2 特征聚合层「隐式」落盘点 —— 实时与离线链路都消费特征：
- FeatureStore：每帧多模型 bbox 特征追加 `{base_dir}/{task_id}/{step_id}/features.jsonl`。
  常开（离线链路硬需求，非可选）。缓冲批量写、best-effort（IO 异常只记日志不抛）。
- FactLedger：L3 产出的事实（EventFact / SegmentFact）追加
  `{base_dir}/{task_id}/{step_id}/facts.jsonl`；`load()` 供离线链路回读（offline 预置）。

落盘目录 `{task_id}/{step_id}/` 与 HLS 同款工作目录（`InferenceManager._db_dir` → base_dir,
见 `hls_strategy._persist_*_segment`），随 step 目录被 cleanup TTL 连带回收。
两者均为 manager 持有的单例：构造时绑定 base_dir，remove_client 时 close(task_id, step_id)。

帧对齐契约：每条记录的 `ts` = 该帧的 `InferenceResult.timestamp`（= 帧捕获 ts，见
`workers/base.py` 构造），与 HLS keypoints/段落盘所用的 `fd.timestamp` 同源同值，
故 feature 行可按 `ts` 精确对上同帧的 HLS 证据片段与 keypoints。
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
    FrameDetections,
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

    def _enqueue(self, task_id: Any, step_id: Any, lines: List[str]) -> None:
        if task_id is None or step_id is None or not lines:
            return
        key = self._key(task_id, step_id)
        with self._lock:
            _, _, buf = self._buffers.setdefault(key, (task_id, step_id, []))
            buf.extend(lines)
            if len(buf) >= self._batch_size:
                _, _, drained = self._buffers.pop(key)
                path = self._path(task_id, step_id)
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

    def flush(self, task_id: Optional[Any] = None, step_id: Optional[Any] = None) -> None:
        """把缓冲写盘。task_id=None 时 flush 全部；否则 flush 指定 (task_id, step_id)。"""
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

    def close(self, task_id: Any, step_id: Any) -> None:
        """任务结束：flush 该 (task, step) 缓冲并丢弃 buffer 槽。"""
        self.flush(task_id, step_id)


class FeatureStore(_JsonlBuffer):
    """多模型 bbox 特征 per-task 落盘（常开）。"""

    def __init__(self, base_dir: Union[str, Path], batch_size: int = 64):
        super().__init__(base_dir, suffix="features", batch_size=batch_size)

    def append(self, task_id: Any, step_id: Any, res) -> None:
        """追加一帧的多模型特征。

        Args:
            task_id: 任务 id（落盘目录键）
            step_id: 洗消步骤 id（落盘目录键，与 HLS 同款 `{task_id}/{step_id}/`）
            res: InferenceResult（duck-typed：.timestamp + .result[task_name]→DetectionOutput）
        """
        if task_id is None or step_id is None:
            return
        try:
            features = {
                task_name: [_serialize_detection(d) for d in out.detections]
                for task_name, out in res.result.items()
            }
            # ts = 帧捕获 ts（res.timestamp），供离线/证据按帧对齐，详见模块 docstring
            record = {"ts": res.timestamp, "features": features}
            line = json.dumps(record, ensure_ascii=False) + "\n"
        except Exception as e:  # 序列化失败 best-effort 跳过
            logger.warning("[FeatureStore] 特征序列化失败 task=%s step=%s: %s", task_id, step_id, e)
            return
        self._enqueue(task_id, step_id, [line])

    def load(self, task_id: Any, step_id: Any, source: str) -> List[FrameDetections]:
        """离线回读：把某 source（检测点/模型名）的全序列特征还原为 DetectionOutput 列表。

        供离线链路使用（offline 预置）。先 flush 缓冲再读，缺失文件返回空。
        注：mask/keypoints/metadata 未落盘，回读的 DetectionOutput.metadata 为空。

        Args:
            task_id: 任务 id
            step_id: 洗消步骤 id
            source: 检测点名（= Detector/Analyzer.name），对应每帧 features[source]
        """
        self.flush(task_id, step_id)
        path = self._path(task_id, step_id)
        if not path.exists():
            return []
        outputs: List[FrameDetections] = []
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
                    outputs.append(FrameDetections(
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

    def append(self, task_id: Any, step_id: Any, facts: List[Union[EventFact, SegmentFact]]) -> None:
        """追加一批事实（offline 预置；online 链路不再写）。"""
        if task_id is None or step_id is None or not facts:
            return
        try:
            lines = [json.dumps(f.to_json(), ensure_ascii=False) + "\n" for f in facts]
        except Exception as e:
            logger.warning("[FactLedger] 事实序列化失败 task=%s step=%s: %s", task_id, step_id, e)
            return
        self._enqueue(task_id, step_id, lines)

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
