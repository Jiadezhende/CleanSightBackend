"""规则表配置：`stages.<step>.compliance` 的原始 dict → `ComplianceConfig`。

    config = parse_config(stage_config.compliance)   # 缺省 / rules 为空 → None；非法 → ValueError

只做解析与自洽校验（字段、组定义、规则 id、message 占位）；类名是否在模型类表里由证据层在核对时校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .evidence import EpisodeParams, EvidenceParams
from .rules import Rule, parse_rules


@dataclass(frozen=True)
class ComplianceConfig:
    evidence: EvidenceParams
    groups: Dict[str, Tuple[str, ...]]
    rules: List[Rule]


def parse_config(spec: Optional[Mapping[str, Any]]) -> Optional[ComplianceConfig]:
    """`stages.<step>.compliance` → 配置；缺省或无规则返回 None。非法 → ValueError。"""
    if not spec or not spec.get("rules"):
        return None
    unknown = set(spec) - {"evidence", "groups", "rules"}
    if unknown:
        raise ValueError(f"compliance 未知字段: {sorted(unknown)}")

    ev_spec = dict(spec.get("evidence") or {})
    episodes = ev_spec.pop("episodes", None) or {}
    if set(episodes) != {"strict", "lenient"}:
        raise ValueError(f"compliance.evidence.episodes 须恰好给 strict / lenient: {sorted(episodes)}")
    if "visible_classes" in ev_spec:
        ev_spec["visible_classes"] = tuple(ev_spec["visible_classes"])
    try:
        evidence = EvidenceParams(
            strict=EpisodeParams(**episodes["strict"]),
            lenient=EpisodeParams(**episodes["lenient"]),
            **ev_spec,
        )
    except TypeError as e:
        raise ValueError(f"compliance.evidence 字段不对: {e}") from e

    groups = {str(g): tuple(cs) for g, cs in (spec.get("groups") or {}).items()}
    empty = [g for g, cs in groups.items() if not cs]
    if empty:
        raise ValueError(f"compliance.groups 有空组: {empty}")

    rules = parse_rules(spec["rules"])
    for rule in rules:
        undefined = [g for g in rule.groups() if g not in groups]
        if undefined:
            raise ValueError(f"规则 {rule.id} 引用了未定义的组 {undefined}，已定义 {sorted(groups)}")
    return ComplianceConfig(evidence, groups, rules)
