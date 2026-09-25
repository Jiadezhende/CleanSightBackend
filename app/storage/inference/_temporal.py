"""L3 时序分析产物 —— `{step}/inference/` 下 `temporal.jsonl` 与 `offline_debug.json` 的读写。

    read_temporal(task, step)                回读全部事实，**落盘序**
    write_temporal(task, step, facts)        整体替换（路线 C）
    write_debug_result(task, step, payload)  离线策略的逐帧中间量，整体替换（路线 C）

事实的货币是 `TemporalEvent | TemporalSegment`（`app.domain.temporal`）；调试产物没有形状，收
`Mapping`——它的键随离线策略变，给了形状就等于让本层认识某个具体模型的中间量。

两条硬约束：

- **`write_temporal` 是整体替换，不是追加。** `temporal.jsonl` 是多写者共居文件（不同 producer 的
  分段、将来的实时打点），**盲写会吃掉别人的事实**：正确姿势是 `read_temporal` → 丢掉自己这个
  producer 的旧条目、其余原样留下 → `write_temporal(合并结果)`。保留谁是 producer 语义，归调用方。
- **`read_temporal` 不排序**，原样返回落盘顺序。两型没有共同时间键（`TemporalEvent.ts` 对
  `TemporalSegment.start`），层没有依据替调用方选。

依赖上界：`app.domain.temporal`（stdlib only）+ stdlib。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Sequence

from app.domain.temporal import TemporalEvent, TemporalSegment
from . import _jsonl, _layout

logger = logging.getLogger(__name__)

# 落盘判别字段 —— 只活在本模块的一对逆运算里。内存里判别是 `isinstance`。
_FACT_EVENT = "event"
_FACT_SEGMENT = "segment"


# ── TemporalEvent / TemporalSegment ↔ 磁盘 record 的对称映射（一对逆运算紧挨放置）─────────────────────────────
#
# 契约：两型都无损落盘（不像 FrameDetection 那样投影），故往返在全字段上闭合。
# `TemporalEvent.value` 与 `meta` 收任意 JSON 值，不可序列化的内容在 encode 时炸，不静默丢。


def _temporal_to_record(fact: TemporalEvent | TemporalSegment) -> Dict[str, Any]:
    """时序事实 → 磁盘 record（逆运算 `_record_to_temporal`）。"""
    if isinstance(fact, TemporalSegment):
        return {
            "type": _FACT_SEGMENT,
            "producer": fact.producer,
            "label": fact.label,
            "start": fact.start,
            "end": fact.end,
            "conf": fact.conf,
            "meta": fact.meta,
        }
    if isinstance(fact, TemporalEvent):
        return {
            "type": _FACT_EVENT,
            "producer": fact.producer,
            "signal": fact.signal,
            "value": fact.value,
            "ts": fact.ts,
            "conf": fact.conf,
            "meta": fact.meta,
        }
    raise TypeError(f"不是 TemporalEvent / TemporalSegment: {type(fact).__name__}")


def _record_to_temporal(rec: Mapping[str, Any]) -> TemporalEvent | TemporalSegment:
    """磁盘 record → 时序事实（`_temporal_to_record` 的逆），按 `type` 判别分派。

    Raises:
        ValueError: `type` 未知或缺失。
        KeyError: 该型的必填字段缺失。
    两者都由 `read_temporal` 当坏行接住（跳过 + warning），不中断其余数据。
    """
    kind = rec.get("type")
    if kind == _FACT_SEGMENT:
        return TemporalSegment(
            producer=rec["producer"],
            label=rec["label"],
            start=float(rec["start"]),
            end=float(rec["end"]),
            conf=float(rec.get("conf", 1.0)),
            meta=rec.get("meta") or {},
        )
    if kind == _FACT_EVENT:
        return TemporalEvent(
            producer=rec["producer"],
            signal=rec["signal"],
            value=rec["value"],
            ts=float(rec["ts"]),
            conf=float(rec.get("conf", 1.0)),
            meta=rec.get("meta") or {},
        )
    raise ValueError(f"未知 fact type: {kind!r}")


# ── temporal.jsonl ───────────────────────────────────────────────────────────────


def read_temporal(task_id: int, step_id: int) -> List[TemporalEvent | TemporalSegment]:
    """回读该 step 的全部事实，**按落盘顺序**（层不排序，理由见模块 docstring）。

    文件不存在返回 `[]`；坏行与形状不对的 record 跳过 + warning。
    """
    path = _layout.domain_dir(task_id, step_id) / _layout.TEMPORAL_NAME
    facts: List[TemporalEvent | TemporalSegment] = []
    for rec in _jsonl.decode(path):
        try:
            facts.append(_record_to_temporal(rec))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("[storage.inference] 跳过形状不对的 fact %s: %s", path, e)
    return facts


def write_temporal(
    task_id: int, step_id: int, facts: Sequence[TemporalEvent | TemporalSegment]
) -> None:
    """**整体替换**该 step 的事实（路线 C：编码 → 同目录 tmp → `os.replace`）。

    调用方须先 `read_temporal` 再合并——本函数不读既有内容，盲写会吃掉别的 producer 的分段与
    所有 `TemporalEvent`。整批先编码完再碰盘，失败时旧文件原样保留（W4）。

    空序列**照写空文件、不删文件**：「跑过、没分出任何段」与「根本没跑过」在盘上要能分开；
    删除是 `delete` 的事。

    Raises:
        TypeError: 序列里有不是 `TemporalEvent` / `TemporalSegment` 的东西，或 `value` / `meta` 不可 JSON 序列化。
        OSError: 建目录 / 写 tmp / 换名失败。是否吞掉由调用方定。
    """
    payload = _jsonl.encode([_temporal_to_record(f) for f in facts])
    path = _layout.domain_dir(task_id, step_id, create=True) / _layout.TEMPORAL_NAME
    _jsonl.write_atomic(path, payload)


# ── offline_debug.json ───────────────────────────────────────────────────────────


def write_debug_result(task_id: int, step_id: int, payload: Mapping[str, Any]) -> None:
    """落一份离线策略的逐帧调试产物（路线 C，整体替换；重复写即覆盖）。

    内容由产出方自定，本层只负责序列化与落位——它是给人看的，故 `indent=2`，也没有配套的
    读函数。

    Raises:
        TypeError: payload 不可 JSON 序列化。
        OSError: 建目录 / 写 tmp / 换名失败。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path = _layout.domain_dir(task_id, step_id, create=True) / _layout.DEBUG_NAME
    _jsonl.write_atomic(path, text)
