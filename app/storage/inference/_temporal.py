"""L3 时序分析产物 —— `{step}/inference/` 下 `temporal.jsonl` 与 `label_probs.npz` 的读写。

    read_temporal(task, step)                回读全部事实，**落盘序**
    write_temporal(task, step, facts)        整体替换（路线 C）
    read_label_probs(task, step)             回读逐帧类别概率；没有则 None
    write_label_probs(task, step, probs)     整体替换（路线 C）

货币都在 `app.domain.temporal`：事实是 `TemporalEvent | TemporalSegment`，逐帧概率是 `LabelProbs`。

两条硬约束：

- **`write_temporal` 是整体替换，不是追加。** `temporal.jsonl` 是多写者共居文件（不同 producer 的
  分段、将来的实时打点），**盲写会吃掉别人的事实**：正确姿势是 `read_temporal` → 丢掉自己这个
  producer 的旧条目、其余原样留下 → `write_temporal(合并结果)`。保留谁是 producer 语义，归调用方。
- **`read_temporal` 不排序**，原样返回落盘顺序。两型没有共同时间键（`TemporalEvent.ts` 对
  `TemporalSegment.start`），层没有依据替调用方选。

依赖上界：`app.domain.temporal` + numpy（`LabelProbs` 的货币）+ stdlib。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from app.domain.temporal import LabelProbs, TemporalEvent, TemporalSegment
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
    task_id: int,
    step_id: int,
    facts: Sequence[TemporalEvent | TemporalSegment],
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


# ── label_probs.npz ──────────────────────────────────────────────────────────────
#
# 三个键：`ts` float64 [T]（无损）、`probs` float16 [T,C]（**有损**，可视化够用，体积减半）、
# `labels` unicode [C]。读写都 `allow_pickle=False`：盘上只有数值与定长字符串，不给反序列化任意对象的口子。

_PROBS_DISK_DTYPE = np.float16


def write_label_probs(task_id: int, step_id: int, probs: LabelProbs) -> None:
    """**整体替换**该 step 的逐帧类别概率（路线 C：同目录 tmp → `os.replace`）。

    只做序列化与落位，不校验形状一致性——那是产出侧的事（本层不认识「合法的概率」）。

    Raises:
        OSError: 建目录 / 写 tmp / 换名失败。失败时 tmp 删除、旧文件原样保留。
    """
    path = _layout.domain_dir(task_id, step_id, create=True) / _layout.LABEL_PROBS_NAME
    _write_probs_atomic(path, probs)


def _write_probs_atomic(path: Path, probs: LabelProbs) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        # 传文件对象而非路径：`np.savez` 收到不以 .npz 结尾的路径会自作主张补后缀，tmp 名就对不上了。
        with open(tmp, "wb") as f:
            np.savez(
                f,
                ts=np.asarray(probs.ts, dtype=np.float64),
                probs=np.asarray(probs.probs).astype(_PROBS_DISK_DTYPE),
                labels=np.asarray(probs.labels, dtype=np.str_),
            )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # 清 tmp 再失败不能盖掉原始错因
            pass
        raise


def read_label_probs(task_id: int, step_id: int) -> Optional[LabelProbs]:
    """回读该 step 的逐帧类别概率；文件不存在返回 `None`。

    `probs` 以 float32 返回（盘上 float16，见上）；`ts` 与写入时位级相等。

    Raises:
        ValueError / KeyError: 文件损坏或缺键。与 jsonl 的逐行容错不同，npz 是整体，坏了就是坏了。
        OSError: 读失败。
    """
    path = _layout.domain_dir(task_id, step_id) / _layout.LABEL_PROBS_NAME
    if not path.exists():
        return None
    with np.load(path, allow_pickle=False) as npz:
        return LabelProbs(
            ts=npz["ts"].astype(np.float64),
            probs=npz["probs"].astype(np.float32),
            labels=tuple(str(x) for x in npz["labels"]),
        )
