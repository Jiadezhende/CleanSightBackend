"""离线规则层的告警 —— `{run}/inference/alarms.jsonl` 的读写。

    write_alarms(run, alarms)     整体替换（路线 C）
    read_alarms(run)              回读，落盘序；文件不存在返回 None

货币是 `app.types.alarm.Alarm`。文件缺失（读出 None）= 没核对过；空文件（读出 []）= 核对过、无告警。
只由离线规则层写；在线告警不落这里（它走 alarm 服务入库）。

依赖上界：`app.types.alarm` + stdlib。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.types.alarm import Alarm, AlarmMetric, AlarmType
from app.types.run import RunIdentity

from . import _jsonl, _layout

logger = logging.getLogger(__name__)


# ── Alarm ↔ 磁盘 record（一对逆运算）──────────────────────────────────────────────
#
# 枚举存 value；`timestamp`（epoch 秒）落 `timestamp_ms`（int），往返精度到毫秒。
# `metadata` 收任意 JSON 值，不可序列化的内容在 encode 时炸，不静默丢。


def _alarm_to_record(alarm: Alarm) -> Dict[str, Any]:
    """告警 → 磁盘 record（逆运算 `_record_to_alarm`）。"""
    if not isinstance(alarm, Alarm):
        raise TypeError(f"不是 Alarm: {type(alarm).__name__}")
    return {
        "alarm_type": AlarmType(alarm.alarm_type).value,
        "alarm_level": alarm.alarm_level,
        "alarm_message": alarm.alarm_message,
        "metric": AlarmMetric(alarm.metric).value,
        "metadata": alarm.metadata,
        "mode": alarm.mode,
        "stage": alarm.stage,
        "seq": alarm.seq,
        "timestamp_ms": int(round(alarm.timestamp * 1000)),
    }


def _record_to_alarm(rec: Mapping[str, Any]) -> Alarm:
    """磁盘 record → 告警（`_alarm_to_record` 的逆）。

    Raises:
        KeyError: 必填字段缺失。
        ValueError: 枚举值未知。
    两者都由 `read_alarms` 当坏行接住（跳过 + warning）。
    """
    return Alarm(
        alarm_type=AlarmType(rec["alarm_type"]),
        alarm_level=rec["alarm_level"],
        alarm_message=rec["alarm_message"],
        metric=AlarmMetric(rec["metric"]),
        metadata=rec.get("metadata") or {},
        mode=rec["mode"],
        stage=rec["stage"],
        seq=int(rec["seq"]),
        timestamp=int(rec["timestamp_ms"]) / 1000,
    )


def read_alarms(run: RunIdentity) -> Optional[List[Alarm]]:
    """回读该 run 的告警，按落盘顺序。文件不存在返回 `None`（没核对过）；坏行与形状不对的 record 跳过 + warning。"""
    path = _layout.domain_dir(run) / _layout.ALARMS_NAME
    if not path.exists():
        return None
    alarms: List[Alarm] = []
    for rec in _jsonl.decode(path):
        try:
            alarms.append(_record_to_alarm(rec))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("[storage.inference] 跳过形状不对的 alarm %s: %s", path, e)
    return alarms


def write_alarms(run: RunIdentity, alarms: Sequence[Alarm]) -> None:
    """**整体替换**该 run 的告警（路线 C）。空序列照写空文件：「核对过、无告警」与「没核对过」要能分开。

    Raises:
        TypeError: 序列里有不是 `Alarm` 的东西，或 `metadata` 不可 JSON 序列化。
        ValueError: `alarm_type` / `metric` 不是合法枚举值。
        OSError: 建目录 / 写 tmp / 换名失败（run 已被回收即 `FileNotFoundError`）。失败时旧文件原样保留。
    """
    payload = _jsonl.encode([_alarm_to_record(a) for a in alarms])
    path = _layout.domain_dir(run, create=True) / _layout.ALARMS_NAME
    _jsonl.write_atomic(path, payload)
