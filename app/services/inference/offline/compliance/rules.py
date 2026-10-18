"""规则层：五种通用规则，每条 `evaluate(ev) -> Verdict`，判定三态。

    rule = parse_rule({"id": ..., "type": "presence" | "duration" | "count" | "order" | "wait", ...})
    verdict = rule.evaluate(evidence)

约束：
- 规则只读 `Evidence`，不碰存储、不碰分割策略。
- 时长 / 次数 / 等待用 strict、lenient 两套实例夹逼：两套都判违规才 SUSPECTED，都判合规才 COMPLIANT。
- `message` 是 `str.format` 模板，占位只许本类型 `VALUES` 里的键与 `at`（相对覆盖起点的 mm:ss），解析时校验。
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Tuple

from .evidence import LENIENT, STRICT, Evidence

COMPLIANT = "compliant"
SUSPECTED = "suspected"
UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class Verdict:
    rule_id: str
    rule_type: str
    state: str
    at: Optional[float] = None                            # 违规时刻（epoch 秒），仅 SUSPECTED 有意义
    values: Dict[str, Any] = field(default_factory=dict)  # 判定用到的数值，也是 message 的占位来源
    reason: str = ""                                      # UNDETERMINED 的原因


@dataclass(frozen=True)
class Rule:
    TYPE: ClassVar[str] = ""
    VALUES: ClassVar[Tuple[str, ...]] = ()

    id: str
    message: str
    level: str = "warning"

    def groups(self) -> Tuple[str, ...]:
        """本规则引用的组名（供解析期核对组是否已定义）。"""
        return ()

    def evaluate(self, ev: Evidence) -> Verdict:
        raise NotImplementedError

    def _verdict(self, state: str, values: Dict[str, Any], at: Optional[float] = None, reason: str = "") -> Verdict:
        return Verdict(self.id, self.TYPE, state, at, values, reason)


@dataclass(frozen=True)
class PresenceRule(Rule):
    """漏步：证据积分 ≥ present_s 合规；≤ absent_s 且可见率达标为违规疑似。"""

    TYPE: ClassVar[str] = "presence"
    VALUES: ClassVar[Tuple[str, ...]] = ("evidence_s", "visible_ratio")

    group: str = ""
    present_s: float = 0.0
    absent_s: float = 0.0
    min_visible_ratio: float = 0.0

    def groups(self):
        return (self.group,)

    def evaluate(self, ev):
        e, vr = ev.evidence_s(self.group), ev.visible_ratio()
        values = {"evidence_s": e, "visible_ratio": vr}
        if e >= self.present_s:
            return self._verdict(COMPLIANT, values)
        if e <= self.absent_s and vr >= self.min_visible_ratio:
            return self._verdict(SUSPECTED, values, ev.coverage[1])
        return self._verdict(UNDETERMINED, values, reason="证据落在死区或可见率不足")


@dataclass(frozen=True)
class DurationRule(Rule):
    """时长不足：激活总时长 strict ≥ min_s 合规；lenient < min_s 且可见率达标为违规疑似。"""

    TYPE: ClassVar[str] = "duration"
    VALUES: ClassVar[Tuple[str, ...]] = ("duration_s", "duration_strict_s", "duration_lenient_s", "visible_ratio")

    group: str = ""
    min_s: float = 0.0
    min_visible_ratio: float = 0.0

    def groups(self):
        return (self.group,)

    def evaluate(self, ev):
        ds, dl = ev.active_duration_s(self.group, STRICT), ev.active_duration_s(self.group, LENIENT)
        vr = ev.visible_ratio()
        values = {"duration_s": dl, "duration_strict_s": ds, "duration_lenient_s": dl, "visible_ratio": vr}
        if ds >= self.min_s:
            return self._verdict(COMPLIANT, values)
        if dl < self.min_s and vr >= self.min_visible_ratio:
            return self._verdict(SUSPECTED, values, ev.coverage[1])
        return self._verdict(UNDETERMINED, values, reason="两套时长跨过阈值或可见率不足")


@dataclass(frozen=True)
class CountRule(Rule):
    """次数不足：strict 计数 ≥ min_count 合规；lenient < min_count 且可见率达标为违规疑似。

    `unit: episode` 数 `group` 的实例；`unit: round_trip` 数 `forward` 段后接 `backward` 段的往返。
    """

    TYPE: ClassVar[str] = "count"
    VALUES: ClassVar[Tuple[str, ...]] = ("count", "count_strict", "count_lenient", "visible_ratio")

    group: str = ""
    min_count: int = 0
    min_visible_ratio: float = 0.0
    unit: str = "episode"
    forward: str = ""
    backward: str = ""

    def __post_init__(self):
        if self.unit not in ("episode", "round_trip"):
            raise ValueError(f"规则 {self.id}: unit 只能是 episode / round_trip: {self.unit!r}")
        if self.unit == "round_trip" and not (self.forward and self.backward):
            raise ValueError(f"规则 {self.id}: unit=round_trip 须给 forward / backward")

    def groups(self):
        return (self.forward, self.backward) if self.unit == "round_trip" else (self.group,)

    def _count(self, ev: Evidence, kind: str) -> int:
        if self.unit == "round_trip":
            return len(ev.round_trips(self.forward, self.backward, kind))
        return len(ev.episodes(self.group, kind))

    def evaluate(self, ev):
        cs, cl = self._count(ev, STRICT), self._count(ev, LENIENT)
        vr = ev.visible_ratio()
        values = {"count": cl, "count_strict": cs, "count_lenient": cl, "visible_ratio": vr}
        if cs >= self.min_count:
            return self._verdict(COMPLIANT, values)
        if cl < self.min_count and vr >= self.min_visible_ratio:
            return self._verdict(SUSPECTED, values, ev.coverage[1])
        return self._verdict(UNDETERMINED, values, reason="两套计数跨过阈值或可见率不足")


@dataclass(frozen=True)
class OrderRule(Rule):
    """乱序：d = anchor(before, before_q) − anchor(after, after_q)；d ≤ 0 合规，d > margin_s 违规疑似。"""

    TYPE: ClassVar[str] = "order"
    VALUES: ClassVar[Tuple[str, ...]] = ("lead_s", "before_at", "after_at")

    before: str = ""
    after: str = ""
    before_q: float = 0.5
    after_q: float = 0.5
    present_s: float = 0.0
    margin_s: float = 0.0

    def groups(self):
        return (self.before, self.after)

    def evaluate(self, ev):
        tb, ta = ev.anchor(self.before, self.before_q), ev.anchor(self.after, self.after_q)
        weak = min(ev.evidence_s(self.before), ev.evidence_s(self.after)) < self.present_s
        if weak or tb is None or ta is None:
            return self._verdict(UNDETERMINED, {}, reason="有一组证据不足，顺序无从比较")
        d = tb - ta
        values = {"lead_s": d, "before_at": tb, "after_at": ta}
        if d <= 0:
            return self._verdict(COMPLIANT, values)
        if d > self.margin_s:
            return self._verdict(SUSPECTED, values, ta)
        return self._verdict(UNDETERMINED, values, reason="先后差落在死区")


@dataclass(frozen=True)
class WaitRule(Rule):
    """等待不足：每个 `after` 实例结束到其后首个 `until` 实例开始的间隔。

    间隔按 lenient 实例算（结束晚、开始早，偏短）都 ≥ min_s 才合规；有一处按 strict 实例算（偏长）仍 < min_s、
    且区间可见率达标即违规疑似。
    """

    TYPE: ClassVar[str] = "wait"
    VALUES: ClassVar[Tuple[str, ...]] = ("wait_s", "wait_strict_s", "wait_lenient_s", "visible_ratio")

    after: str = ""
    until: Tuple[str, ...] = ()
    min_s: float = 0.0
    min_visible_ratio: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "until", tuple(self.until))
        if not self.until:
            raise ValueError(f"规则 {self.id}: until 不能为空")

    def groups(self):
        return (self.after,) + self.until

    def _next_start(self, ev: Evidence, kind: str, t: float) -> Optional[float]:
        """`until` 各组 kind 实例里，结束晚于 t 的最早起点。"""
        starts = [e.start for g in self.until for e in ev.episodes(g, kind) if e.end > t]
        return min(starts) if starts else None

    def evaluate(self, ev):
        afters_s = ev.episodes(self.after, STRICT)
        checked, compliant = 0, True
        for e in ev.episodes(self.after, LENIENT):
            nxt = self._next_start(ev, LENIENT, e.end)
            if nxt is None:
                continue
            checked += 1
            gl = nxt - e.end
            overlap = [s for s in afters_s if s.start <= e.end and s.end >= e.start]
            gs = None
            if overlap:
                nxt_s = self._next_start(ev, STRICT, overlap[-1].end)
                gs = None if nxt_s is None else nxt_s - overlap[-1].end
            vr = ev.visible_ratio(e.end, nxt)
            values = {"wait_s": gs if gs is not None else gl, "wait_strict_s": gs, "wait_lenient_s": gl,
                      "visible_ratio": vr}
            if gs is not None and gs < self.min_s and vr >= self.min_visible_ratio:
                return self._verdict(SUSPECTED, values, nxt)
            compliant = compliant and gl >= self.min_s
        if checked == 0:
            return self._verdict(UNDETERMINED, {}, reason=f"没有其后跟着 {list(self.until)} 的 {self.after} 实例")
        if compliant:
            return self._verdict(COMPLIANT, {})
        return self._verdict(UNDETERMINED, {}, reason="间隔落在两套实例之间")


RULE_TYPES: Dict[str, type] = {
    cls.TYPE: cls for cls in (PresenceRule, DurationRule, CountRule, OrderRule, WaitRule)
}


def parse_rule(spec: Mapping[str, Any]) -> Rule:
    """YAML 一条规则 → Rule。类型未知、字段多余或缺失、message 占位非法 → ValueError。"""
    spec = dict(spec)
    rule_type = spec.pop("type", None)
    cls = RULE_TYPES.get(rule_type)
    if cls is None:
        raise ValueError(f"规则 {spec.get('id')!r}: 未知 type {rule_type!r}，可选 {sorted(RULE_TYPES)}")
    known = {f.name for f in fields(cls)}
    unknown = set(spec) - known
    if unknown:
        raise ValueError(f"规则 {spec.get('id')!r}: 未知字段 {sorted(unknown)}")
    if not spec.get("id") or not spec.get("message"):
        raise ValueError(f"规则缺 id 或 message: {spec}")
    rule = cls(**spec)
    allowed = set(cls.VALUES) | {"at"}
    used = {name.split(".")[0].split("[")[0] for _, name, _, _ in string.Formatter().parse(rule.message) if name}
    if used - allowed:
        raise ValueError(f"规则 {rule.id}: message 占位 {sorted(used - allowed)} 不可用，可选 {sorted(allowed)}")
    return rule


def render_message(rule: Rule, verdict: Verdict, coverage_start: float) -> str:
    """渲染告警文案；`at` 为违规时刻相对覆盖起点的 mm:ss。"""
    at = "--:--"
    if verdict.at is not None:
        sec = max(0, int(verdict.at - coverage_start))
        at = f"{sec // 60:02d}:{sec % 60:02d}"
    return rule.message.format(at=at, **{k: verdict.values.get(k) for k in rule.VALUES})


def parse_rules(specs: List[Mapping[str, Any]]) -> List[Rule]:
    rules = [parse_rule(s) for s in specs]
    ids = [r.id for r in rules]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"规则 id 重复: {dup}")
    return rules
