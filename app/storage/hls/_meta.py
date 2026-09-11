"""`metadata.json` —— 本域的段统计（两轨共用一份）。

    {
      "task_id": 1, "step_id": 2,
      "start_time": 1700000000, "end_time": null,
      "raw_segments":       {"count": 3, "total_duration": 30.04,
                             "first_timestamp": 1700000000.1, "last_timestamp": 1700000020.2},
      "processed_segments": {...同构...},
      "created_at": "...", "updated_at": "..."
    }

两个消费方：大屏/清单要"这个 step 有多少段多长"，`cleanup_worker` 拿 `updated_at` 当
TTL 判据。**它是派生量不是真值**——段时长真值在 playlist 的 EXTINF 里，这里的
`total_duration` 只是同一批数的累加缓存。

**整体读改写（规范 §7.2 路线 C）**：内容由层内序列化、没有"追加一行"的形态，故
tmp + `os.replace` 换整份。`end_time` 字段从来只写 `null`（没有写侧知道 step 何时结束），
保留是因为读侧还在按这个形状解析。

依赖上界：stdlib only。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_TMP_SUFFIX = ".tmp"


def _empty_track_stats() -> Dict[str, Any]:
    return {
        "count": 0,
        "total_duration": 0.0,
        "first_timestamp": None,
        "last_timestamp": None,
    }


def _init_document(task_id: int, step_id: int, timestamp: float) -> Dict[str, Any]:
    now = datetime.now().isoformat()
    return {
        "task_id": task_id,
        "step_id": step_id,
        "start_time": int(timestamp),
        "end_time": None,
        "raw_segments": _empty_track_stats(),
        "processed_segments": _empty_track_stats(),
        "created_at": now,
        "updated_at": now,
    }


def _load(path: Path, task_id: int, step_id: int, timestamp: float) -> Dict[str, Any]:
    """读现有统计；不存在或解析不出时给一份新的。

    **解析不出就重建 + warning，不抛**：这是派生量，段与 playlist 才是真值。为一份统计
    缓存中断整段落盘（连带丢掉视频）是拿主产物给账本陪葬；重建的代价只是历史计数归零。
    """
    if not path.exists():
        return _init_document(task_id, step_id, timestamp)
    try:
        with path.open("r", encoding="utf-8") as f:
            document = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("[storage.hls] metadata 读不出，按新建处理 %s: %s", path, e)
        return _init_document(task_id, step_id, timestamp)
    if not isinstance(document, dict):
        logger.warning("[storage.hls] metadata 不是对象，按新建处理 %s", path)
        return _init_document(task_id, step_id, timestamp)
    return document


def record_segment(
    path: Path,
    *,
    task_id: int,
    step_id: int,
    track: str,
    duration_s: float,
    timestamp: float,
) -> None:
    """把一个刚登记的段计入统计（每次调用记一段）。

    Raises:
        OSError: 写失败。段本身此刻已登记完毕（W8 把本步排在最后），故抛出去只意味着
            "统计落后了一段"，不是产物残缺。

    段数恒 +1 而不做成参数：本函数的调用点只有"刚 insert 完一段"这一处，给它一个
    `count_delta` 只是把一个常量 1 搬到调用方去写。
    """
    document = _load(path, task_id, step_id, timestamp)

    key = f"{track}_segments"
    stats = document.get(key)
    if not isinstance(stats, dict):
        stats = _empty_track_stats()
        document[key] = stats

    stats["count"] = stats.get("count", 0) + 1
    stats["total_duration"] = stats.get("total_duration", 0.0) + duration_s
    if stats.get("first_timestamp") is None:
        stats["first_timestamp"] = timestamp
    stats["last_timestamp"] = timestamp

    document["updated_at"] = datetime.now().isoformat()

    tmp = path.with_suffix(_TMP_SUFFIX)
    payload = json.dumps(document, ensure_ascii=False, indent=2)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
