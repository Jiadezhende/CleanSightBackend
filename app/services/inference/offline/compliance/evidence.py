"""证据层：逐帧类别概率 + 检测结果 → 规则可读的量。

    ev = build_evidence(probs, frames, groups, params)
    ev.evidence_s("flush") / ev.visible_ratio() / ev.anchor("short_brush", 0.9)
    ev.episodes("flush", STRICT) / ev.active_duration_s("short_brush", LENIENT) / ev.round_trips("insert", "withdraw", STRICT)

约束：
- 时间一律是帧捕获墙钟 ts（epoch 秒，与 `LabelProbs.ts` / `FrameDetection.ts` 同轴）。
- 组引用的类名必须都在 `LabelProbs.labels` 里，否则 `ValueError`（换模型后类表不符要当场报出来）。
- 实例切分有 strict / lenient 两套参数：strict 进入阈值高、丢得多（偏少计、偏短），lenient 反之。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from app.types.detection import FrameDetection
from app.types.temporal import LabelProbs

STRICT = "strict"
LENIENT = "lenient"


@dataclass(frozen=True)
class EpisodeParams:
    p_on: float         # 平滑后组概率 ≥ 它进入实例
    p_off: float        # < 它退出实例（滞回）
    merge_gap_s: float  # 同组实例间隔小于它即合并
    min_len_s: float    # 合并后短于它的实例丢弃；往返计数里方向段也按它过滤

    def __post_init__(self):
        if not 0.0 <= self.p_off <= self.p_on <= 1.0:
            raise ValueError(f"需 0 <= p_off <= p_on <= 1: p_on={self.p_on} p_off={self.p_off}")
        if self.merge_gap_s < 0 or self.min_len_s < 0:
            raise ValueError(f"merge_gap_s / min_len_s 不能为负: {self.merge_gap_s} / {self.min_len_s}")


@dataclass(frozen=True)
class EvidenceParams:
    strict: EpisodeParams
    lenient: EpisodeParams
    smooth_s: float = 1.0
    p_floor: float = 0.3
    visible_classes: Tuple[str, ...] = ("scope_control_body", "scope_mid_section")
    visible_window_s: float = 1.0
    gap_s: float = 2.0  # 逐帧时长上限：detections 断档处不把空档算进时长

    def episode_params(self, kind: str) -> EpisodeParams:
        if kind == STRICT:
            return self.strict
        if kind == LENIENT:
            return self.lenient
        raise ValueError(f"未知实例口径: {kind!r}")


@dataclass(frozen=True)
class Episode:
    """一段实例：帧下标 [i0, i1)，`start` / `end` 为首末帧 ts（闭区间）。"""

    i0: int
    i1: int
    start: float
    end: float


class Evidence:
    """一个 run 的证据。构造后只读；各派生量按需计算。"""

    def __init__(
        self,
        ts: np.ndarray,
        dt: np.ndarray,
        visible: np.ndarray,
        coverage: Tuple[float, float],
        group_probs: Mapping[str, np.ndarray],
        params: EvidenceParams,
    ):
        self.ts = ts
        self.dt = dt
        self.visible = visible
        self.coverage = coverage
        self._p = dict(group_probs)
        self.params = params

    # ── 积分类 ──────────────────────────────────────────────────────────────────

    def _contrib(self, group: str) -> np.ndarray:
        p = self._p[group]
        return np.where((p >= self.params.p_floor) & self.visible, p * self.dt, 0.0)

    def evidence_s(self, group: str) -> float:
        """可见帧上、概率不低于 `p_floor` 的组概率积分（期望动作秒数）。"""
        return float(self._contrib(group).sum())

    def anchor(self, group: str, q: float) -> Optional[float]:
        """累计证据首次达到 `q` 成的 ts；证据为 0 时 None。"""
        cum = np.cumsum(self._contrib(group))
        if cum.size == 0 or cum[-1] <= 0:
            return None
        return float(self.ts[int(np.searchsorted(cum, q * cum[-1], side="left"))])

    def visible_ratio(self, start: Optional[float] = None, end: Optional[float] = None) -> float:
        """[start, end] 内可见时长 / 区间时长；缺省为整个覆盖区间。区间长度 ≤ 0 时为 1。"""
        a = self.coverage[0] if start is None else start
        b = self.coverage[1] if end is None else end
        if b <= a:
            return 1.0
        inside = (self.ts >= a) & (self.ts <= b)
        return float(min(1.0, (self.dt * (self.visible & inside)).sum() / (b - a)))

    # ── 实例类 ──────────────────────────────────────────────────────────────────

    def _active(self, p: np.ndarray, kind: str) -> np.ndarray:
        prm = self.params.episode_params(kind)
        active = np.zeros(p.size, dtype=bool)
        on = False
        for i, v in enumerate(p):
            on = v >= prm.p_off if on else v >= prm.p_on
            active[i] = on
        return active

    def episodes(self, group: str, kind: str) -> List[Episode]:
        """滞回切出的实例：间隔 < `merge_gap_s` 合并，合并后短于 `min_len_s` 丢弃。"""
        return self._episodes(self._p[group], kind)

    def _episodes(self, p: np.ndarray, kind: str) -> List[Episode]:
        prm = self.params.episode_params(kind)
        runs = _runs(self._active(p, kind))
        merged: List[List[int]] = []
        for i0, i1 in runs:
            if merged and self.ts[i0] - self.ts[merged[-1][1] - 1] < prm.merge_gap_s:
                merged[-1][1] = i1
            else:
                merged.append([i0, i1])
        return [
            Episode(i0, i1, float(self.ts[i0]), float(self.ts[i1 - 1]))
            for i0, i1 in merged
            if self.dt[i0:i1].sum() >= prm.min_len_s
        ]

    def active_duration_s(self, group: str, kind: str) -> float:
        """各实例内处于激活态的帧时长之和（合并跨过的空档不计）。"""
        p = self._p[group]
        active = self._active(p, kind)
        return float(sum(self.dt[e.i0:e.i1][active[e.i0:e.i1]].sum() for e in self._episodes(p, kind)))

    def round_trips(self, forward: str, backward: str, kind: str) -> List[Tuple[float, float]]:
        """往返：`forward ∪ backward` 的实例帧上按两组概率大小定方向，短于 `min_len_s` 的方向段丢弃、
        同向相邻段合并，「forward 段后接 backward 段」计一次。返回各次 (forward 起点, backward 终点)。
        """
        prm = self.params.episode_params(kind)
        pf, pb = self._p[forward], self._p[backward]
        frames = [i for e in self._episodes(pf + pb, kind) for i in range(e.i0, e.i1)]
        if not frames:
            return []
        idx = np.asarray(frames)
        is_fwd = pf[idx] >= pb[idx]
        # 方向段：(方向, 时长, 首帧 ts, 末帧 ts)，按 idx 上的相邻关系切（跨实例空档也连着看）
        segs = []
        for j0, j1 in _runs_of_values(is_fwd):
            segs.append([bool(is_fwd[j0]), float(self.dt[idx[j0:j1]].sum()),
                         float(self.ts[idx[j0]]), float(self.ts[idx[j1 - 1]])])
        kept: List[List] = []
        for s in segs:
            if s[1] < prm.min_len_s:
                continue
            if kept and kept[-1][0] == s[0]:
                kept[-1][1] += s[1]
                kept[-1][3] = s[3]
            else:
                kept.append(s)
        return [(a[2], b[3]) for a, b in zip(kept, kept[1:]) if a[0] and not b[0]]


def build_evidence(
    probs: LabelProbs,
    frames: Sequence[FrameDetection],
    groups: Mapping[str, Sequence[str]],
    params: EvidenceParams,
) -> Evidence:
    """组装一个 run 的证据。`frames` 只用来算可见性与覆盖区间。"""
    labels = list(probs.labels)
    missing = {g: [c for c in cs if c not in labels] for g, cs in groups.items()}
    missing = {g: cs for g, cs in missing.items() if cs}
    if missing:
        raise ValueError(f"规则组引用了模型类表里没有的类: {missing}；模型类表: {labels}")

    ts = np.asarray(probs.ts, dtype=np.float64)
    p = np.asarray(probs.probs, dtype=np.float64)
    dt = _frame_dt(ts, params.gap_s)
    window = _smooth_window(dt, params.smooth_s)
    group_probs: Dict[str, np.ndarray] = {
        g: _smooth(p[:, [labels.index(c) for c in cs]].sum(axis=1), window) for g, cs in groups.items()
    }

    visible_ts = np.asarray(sorted(
        f.ts for f in frames
        if any(b.class_name in params.visible_classes for out in f.by_source.values() for b in out.boxes)
    ), dtype=np.float64)
    visible = _near(ts, visible_ts, params.visible_window_s)

    if frames:
        coverage = (float(frames[0].ts), float(frames[-1].ts))
    elif ts.size:
        coverage = (float(ts[0]), float(ts[-1]))
    else:
        coverage = (0.0, 0.0)
    return Evidence(ts, dt, visible, coverage, group_probs, params)


def _frame_dt(ts: np.ndarray, cap: float) -> np.ndarray:
    if ts.size < 2:
        return np.zeros(ts.size)
    d = np.diff(ts)
    d = np.append(d, np.median(d))
    return np.clip(d, 0.0, cap)


def _smooth_window(dt: np.ndarray, smooth_s: float) -> int:
    step = float(np.median(dt)) if dt.size and np.median(dt) > 0 else 0.0
    n = int(round(smooth_s / step)) if step > 0 else 1
    return max(1, n | 1)  # 奇数，居中


def _smooth(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or x.size == 0:
        return x
    kernel = np.ones(window)
    return np.convolve(x, kernel, mode="same") / np.convolve(np.ones(x.size), kernel, mode="same")


def _near(ts: np.ndarray, targets: np.ndarray, window: float) -> np.ndarray:
    """ts 每帧 ±window 内是否有 targets 中的点（targets 已升序）。"""
    if targets.size == 0:
        return np.zeros(ts.size, dtype=bool)
    lo = np.searchsorted(targets, ts - window, side="left")
    hi = np.searchsorted(targets, ts + window, side="right")
    return hi > lo


def _runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """True 连续段的 [i0, i1)。"""
    padded = np.concatenate([[False], mask, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist()))


def _runs_of_values(values: np.ndarray) -> List[Tuple[int, int]]:
    """值不变的连续段的 [j0, j1)。"""
    if values.size == 0:
        return []
    bounds = np.flatnonzero(values[1:] != values[:-1]) + 1
    starts = np.concatenate([[0], bounds])
    ends = np.concatenate([bounds, [values.size]])
    return list(zip(starts.tolist(), ends.tolist()))
