"""核对编排：读 run 的离线产物 → 证据 → 规则判定 → 违规疑似转 `Alarm` 落 `alarms.jsonl`。

    result = check_run(run, stage_config.compliance, stage=stage_config.alias)                # 写 alarms.jsonl（整体替换）
    result = check_run(run, stage_config.compliance, stage=stage_config.alias, dry_run=True)  # 不写盘，带全部判定

约束：
- 规则表缺省或 `rules` 为空 → skipped；没有 `label_probs.npz` → skipped。skipped 不碰 `alarms.jsonl`
  （同离线 Runner「跳过不覆盖旧事实」）。
- 配置非法、类表不符 → `ValueError` 上抛；落盘失败 `OSError` 上抛。
- 只读盘上产物（经 `app.storage.inference`），不 import inference、不 import torch。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.types.alarm import ALARM_MODE_SETTLEMENT, Alarm, AlarmMetric, AlarmType
from app.types.detection import FrameDetection
from app.types.run import RunIdentity
from app.types.temporal import LabelProbs, TemporalSegment

from .config import ComplianceConfig, parse_config
from .evidence import Evidence, build_evidence
from .rules import SUSPECTED, Verdict, render_message

_METRICS = {
    "presence": AlarmMetric.STEP_MISSING,
    "duration": AlarmMetric.STEP_DURATION,
    "count": AlarmMetric.STEP_COUNT,
    "order": AlarmMetric.STEP_ORDER,
    "wait": AlarmMetric.STEP_WAIT,
}


@dataclass(frozen=True)
class CheckResult:
    """status ∈ {completed, skipped}；skipped 时 alarm_count 为 None。"""

    status: str
    alarm_count: Optional[int]
    message: str = ""
    verdicts: List[Verdict] = field(default_factory=list)


def evaluate(
    config: ComplianceConfig, probs: LabelProbs, frames: Sequence[FrameDetection]
) -> Tuple[Evidence, List[Verdict]]:
    """纯函数：证据 + 逐条判定（规则表顺序）。"""
    ev = build_evidence(probs, frames, config.groups, config.evidence)
    return ev, [rule.evaluate(ev) for rule in config.rules]


def to_alarms(
    config: ComplianceConfig,
    verdicts: Sequence[Verdict],
    ev: Evidence,
    *,
    stage: str,
    producer: Optional[str],
) -> List[Alarm]:
    """只把 SUSPECTED 转成告警；seq 为本 run 内序号。

    `metadata["at_ms"]` 是违规时刻（可回看定位）；整段判定的规则没有时刻，为 None，`timestamp` 取覆盖末端。
    """
    rules = {r.id: r for r in config.rules}
    alarms: List[Alarm] = []
    for v in verdicts:
        if v.state != SUSPECTED:
            continue
        rule = rules[v.rule_id]
        alarms.append(Alarm(
            alarm_type=AlarmType.PROCESS_VIOLATION,
            alarm_level=rule.level,
            alarm_message=render_message(rule, v, ev.coverage[0]),
            metric=_METRICS[v.rule_type],
            metadata={
                "rule_id": v.rule_id,
                "rule_type": v.rule_type,
                "at_ms": None if v.at is None else _ms(v.at),
                "producer": producer,
                "coverage_start_ms": _ms(ev.coverage[0]),
                "coverage_end_ms": _ms(ev.coverage[1]),
                **_metadata_values(v.values),
            },
            mode=ALARM_MODE_SETTLEMENT,
            stage=stage,
            seq=len(alarms),
            timestamp=v.at if v.at is not None else ev.coverage[1],
        ))
    return alarms


def check_run(
    run: RunIdentity, spec: Optional[Mapping[str, Any]], *, stage: str, dry_run: bool = False
) -> CheckResult:
    """核对一个 run。`spec` 是该 step 的规则表原始 dict，`stage` 是写进告警的阶段别名。"""
    from app.storage import inference as inference_store

    config = parse_config(spec)
    if config is None:
        return CheckResult("skipped", None)
    probs = inference_store.read_label_probs(run)
    if probs is None:
        return CheckResult("skipped", None, "该 run 没有逐帧类别概率（label_probs.npz），无法核对")
    frames = inference_store.read_detections(run)
    producer = next(
        (f.producer for f in inference_store.read_temporal(run) if isinstance(f, TemporalSegment)), None
    )

    ev, verdicts = evaluate(config, probs, frames)
    alarms = to_alarms(config, verdicts, ev, stage=stage, producer=producer)
    if not dry_run:
        inference_store.write_alarms(run, alarms)
    return CheckResult("completed", len(alarms), verdicts=verdicts)


def _ms(ts: float) -> int:
    return int(round(ts * 1000))


def _metadata_values(values: Mapping[str, Any]) -> Dict[str, Any]:
    """判定数值进 metadata：`*_at` 时刻换成 `*_at_ms`（int），浮点保留 3 位。"""
    out: Dict[str, Any] = {}
    for k, v in values.items():
        if k.endswith("_at"):
            out[f"{k}_ms"] = None if v is None else _ms(v)
        elif isinstance(v, float):
            out[k] = round(v, 3)
        else:
            out[k] = v
    return out
