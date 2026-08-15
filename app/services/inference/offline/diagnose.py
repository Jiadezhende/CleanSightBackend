"""特征健康诊断 —— 找出无效（恒定 / 重复）的特征列。

「无效特征」分两类，**检测方法完全不同，不能混为一谈**：

1. **结构性无效**：特征契约声明了检测器永远不会产出的类别，于是那些列在**任何数据上**
   都恒零。`short_brush` / `long_brush` / `brush_tip_out` 曾属此类——白占 45 列输入维度、
   让 normalizer 的 std=0、给模型喂纯噪声维度。
   → 靠 `check_object_contract` **先验**捕获：拿 OBJECTS 比对检测器 checkpoint 的实际
     类别名。这是配置期就能发现的契约错，不必等导出完再统计。

2. **数据相关无效**：该片段恰好没出现这个目标（如只含刷洗、不含灌注的片段里 syringe
   恒零）。这**不是缺陷**，换个片段就是活的。
   → 靠 `scan_columns` **跨 step 聚合**判定：只在部分 step 恒定 = 数据相关；在**全部**
     step 都恒定 = 结构性可疑。

**单个 step 的数据分不出这两类**——这是本模块要求跨 step 扫描的唯一理由。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 判定「常量列」的阈值：标准差低于此值即视为无变化。不用 ==0 是因为浮点特征
# （如插值补出的 conf）可能有极微抖动，实质仍不携带信息。
_CONST_STD = 1e-8
# 判定「重复列」的相关系数阈值。
_DUP_CORR = 0.9999


@dataclass
class ColumnHealth:
    """单个特征列跨 step 的健康度。"""

    name: str
    steps_seen: int = 0
    steps_constant: int = 0

    @property
    def always_constant(self) -> bool:
        """在所有见过的 step 里都恒定 → 结构性可疑。"""
        return self.steps_seen > 0 and self.steps_constant == self.steps_seen

    @property
    def sometimes_constant(self) -> bool:
        """只在部分 step 恒定 → 数据相关，正常现象。"""
        return 0 < self.steps_constant < self.steps_seen


@dataclass
class TagReport:
    """一个 Segmenter（= 一套特征 × 一个网络）的诊断结果。"""

    tag: str
    steps: List[str] = field(default_factory=list)
    columns: Dict[str, ColumnHealth] = field(default_factory=dict)
    duplicate_pairs: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def structural_suspects(self) -> List[str]:
        return [n for n, c in self.columns.items() if c.always_constant]

    @property
    def data_dependent(self) -> List[str]:
        return [n for n, c in self.columns.items() if c.sometimes_constant]


def scan_columns(
    export_root: Path, tag_filter: Optional[str] = None
) -> Dict[str, TagReport]:
    """扫描导出产物，按 Segmenter 聚合各列的恒定情况。

    产物布局是 `{root}/{task}/{step}/manifest_{Tag}.json` + 同目录 `input_{Tag}.npz`。

    Args:
        export_root: 产物根目录（`{offline_base_dir}/.cache`）
        tag_filter: 只看某个 Segmenter（如 `CleanBiGRUSegmenter`）；None = 全部

    Returns:
        {tag: TagReport}。**只有 steps ≥ 2 时「结构性可疑」才有判定力**——单个 step 上
        恒定的列既可能是契约错，也可能只是这段视频没出现该目标。
    """
    reports: Dict[str, TagReport] = {}
    for manifest_path in sorted(Path(export_root).glob("*/*/manifest_*.json")):
        d = manifest_path.parent
        tag = manifest_path.stem[len("manifest_"):]
        if tag_filter and tag != tag_filter:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            features = np.load(d / f"input_{tag}.npz")["features"]
        except Exception as e:
            logger.warning("[Diagnose] 跳过不可读产物 %s: %s", manifest_path, e)
            continue

        names = list(manifest.get("feature_names") or [])
        if features.ndim != 2 or features.shape[1] != len(names):
            logger.warning("[Diagnose] 跳过列名与矩阵不匹配的产物 %s", manifest_path)
            continue

        report = reports.setdefault(tag, TagReport(tag=tag))
        report.steps.append(f"{d.parent.name}/{d.name}")
        std = features.std(axis=0)
        for name, s in zip(names, std):
            col = report.columns.setdefault(name, ColumnHealth(name=name))
            col.steps_seen += 1
            if s <= _CONST_STD:
                col.steps_constant += 1
        report.duplicate_pairs = _find_duplicates(features, names)
    return reports


def _find_duplicates(features: np.ndarray, names: Sequence[str]) -> List[Tuple[str, str]]:
    """找出内容近乎重复的列对（|corr| ≥ 阈值）。恒定列不参与——它们已由恒定判定覆盖。"""
    std = features.std(axis=0)
    alive = np.where(std > _CONST_STD)[0]
    if len(alive) < 2:
        return []
    corr = np.corrcoef(features[:, alive], rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0)
    pairs: List[Tuple[str, str]] = []
    for a in range(len(alive)):
        for b in range(a + 1, len(alive)):
            if abs(corr[a, b]) >= _DUP_CORR:
                pairs.append((names[alive[a]], names[alive[b]]))
    return pairs


def check_object_contract(
    objects: Sequence[str], checkpoints: Sequence[Path]
) -> Tuple[List[str], List[str]]:
    """**先验**契约检查：特征契约里的目标类别 vs 检测器实际会产出的类别名。

    这是捕获结构性无效特征的正道——`short_brush` 那类问题在配置期就能发现，
    不必等跑完导出再从矩阵里统计。

    Args:
        objects: 特征契约声明的目标类别（如 blocks/bbox.py 的 OBJECTS）
        checkpoints: 部署中的检测器权重路径

    Returns:
        (never_produced, never_consumed)
        never_produced: 契约里声明、但没有任何检测器会产出 → **这些列必然恒零**
        never_consumed: 检测器会产出、但契约没消费 → 白丢的检测信号
    """
    produced: set = set()
    for ckpt in checkpoints:
        path = Path(ckpt)
        if not path.exists():
            logger.warning("[Diagnose] checkpoint 不存在，跳过 %s", path)
            continue
        from ultralytics import YOLO  # 重依赖，仅诊断时引入

        produced.update(str(n) for n in YOLO(str(path)).model.names.values())
    declared = set(objects)
    return sorted(declared - produced), sorted(produced - declared)


def format_report(reports: Dict[str, TagReport]) -> str:
    """把诊断结果渲染成可读文本。"""
    if not reports:
        return "未找到任何导出产物。"
    lines: List[str] = []
    for tag, r in sorted(reports.items()):
        lines.append(f"\n=== {tag} （{len(r.steps)} 个 step: {', '.join(r.steps)}）===")
        total = len(r.columns)
        suspects, data_dep = r.structural_suspects, r.data_dependent
        lines.append(f"  特征列 {total}")
        if len(r.steps) < 2:
            lines.append(
                f"  ⚠ 只有 1 个 step，无法区分「结构性无效」与「数据相关」——"
                f"恒定列 {len(suspects)} 个仅供参考，需至少 2 个 step 才有判定力"
            )
        lines.append(f"  全 step 恒定（结构性可疑，应从契约移除）: {len(suspects)}")
        for n in suspects:
            lines.append(f"      {n}")
        lines.append(f"  部分 step 恒定（数据相关，正常）: {len(data_dep)}")
        for n in data_dep:
            c = r.columns[n]
            lines.append(f"      {n}  ({c.steps_constant}/{c.steps_seen} 个 step 恒定)")
        if r.duplicate_pairs:
            lines.append(f"  近乎重复的列对（|corr|≥{_DUP_CORR}）: {len(r.duplicate_pairs)}")
            for a, b in r.duplicate_pairs[:20]:
                lines.append(f"      {a}  ==  {b}")
            if len(r.duplicate_pairs) > 20:
                lines.append(f"      …… 另 {len(r.duplicate_pairs) - 20} 对")
    return "\n".join(lines)
