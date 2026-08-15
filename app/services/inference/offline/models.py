"""离线链路的唯一货币壳。

一个 `FeatureBlock` 同时承担三种身份，故不再有第二个壳：

    ① 块层产物   blocks.load(BlockKind.BBOX / VGLOBAL, ...) 的返回值
    ② 模型输入   Segmenter.build_input() 把若干块拼成的那一块（原 ModelInput）
    ③ 落盘单元   .cache 里 npz 的内存形态

它替掉了原先的三个壳：
    VisualFrames   —— 揉了帧级与手级两个粒度，且 valid / hand_mask 命名不一致（极性坑，见下）
    ModelInput     —— 就是一块 concat 后的特征 + 几个标量
    ExportQuality  —— 是 FrameSource.FetchStats 的冗余重壳，还抄漏了 decode_short

本模块刻意只依赖 numpy + dataclass：import 它不该拖起 torch。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class FeatureBlock:
    """一块逐帧特征。

    Attributes:
        values: `[T, C]`（2D 块）或 `[T, K, C]`（3D 块，如将来的 hand tokens）
        names: 通道名，`len == C`。**不是装饰**——加载 checkpoint 时与权重内记录的
            feature_names 逐项比对，不一致即硬失败（防 train/serve skew 的实际门禁）；
            导出诊断也按列名找恒定/无效特征
        ts: 每行对应的帧时间戳，**唯一 join key**（需求 F1）
        valid: 形状 = `values` 去掉通道维（`[T]` 或 `[T, K]`）。**None = 该块恒有效**。
            语义恒为「True = 真实有效」，见下方极性说明
        version: 特征版本串，与 checkpoint 内记录的 feature_version 对齐
        spans: 各来源块在列上的区间 `{块名: [起, 止)}`，供训练仓按分支切开做等宽投影/
            门控/交叉注意力。拼接本身无损，但让下游靠列名前缀猜是脆的

    极性约定（硬不变式 N7）：有效性数组一律命名 `*_valid`，**True = 有效**。禁止叫
    `*_mask`——PyTorch 的 `key_padding_mask` / `attn_mask` 约定是 True = 忽略，极性
    正好相反；直接传进去会屏蔽掉所有真实位置而 loss 照常下降，**静默出错**。极性转换
    由消费侧显式写 `~valid` 完成，那个取反动作本身就是提醒。

    刻意**没有** fps 字段：可由 `ts` 反推（`blocks.bbox.effective_fps`），存一份就会漂。
    也刻意**不带**取帧质量统计：那只有日志与 npz 头两个消费者，没有内存消费者，直接
    用 `FetchStats` 即可。
    """

    values: np.ndarray
    names: List[str]
    ts: List[float]
    valid: Optional[np.ndarray] = None
    version: str = ""
    spans: Dict[str, List[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.values.ndim < 2:
            raise ValueError(f"FeatureBlock.values 至少二维，收到 {self.values.shape}")
        if len(self.ts) != self.values.shape[0]:
            raise ValueError(
                f"FeatureBlock ts 与 values 帧数不一致: {len(self.ts)} vs {self.values.shape[0]}"
            )
        if len(self.names) != self.values.shape[-1]:
            raise ValueError(
                f"FeatureBlock names 与通道数不一致: {len(self.names)} vs {self.values.shape[-1]}"
            )
        if self.valid is not None and tuple(self.valid.shape) != tuple(self.values.shape[:-1]):
            raise ValueError(
                f"FeatureBlock valid 形状应为 values 去掉通道维 {self.values.shape[:-1]}，"
                f"收到 {self.valid.shape}"
            )

    @property
    def frame_count(self) -> int:
        return int(self.values.shape[0])

    @property
    def feature_dim(self) -> int:
        return int(self.values.shape[-1])

    def replace_values(
        self,
        values: np.ndarray,
        names: Sequence[str],
        version: str,
        spans: Optional[Dict[str, List[int]]] = None,
    ) -> "FeatureBlock":
        """换一套特征/列名/版本重建一块（含 NaN/inf 兜底）。

        `spans` 不传则沿用原块声明——适用于只在块内追加列的变换。
        """
        return FeatureBlock(
            values=finite(values),
            names=list(names),
            ts=list(self.ts),
            valid=self.valid,
            version=version,
            spans=dict(self.spans) if spans is None else spans,
        )

    def header(self) -> Dict[str, Any]:
        """npz 头里的非数组字段（names / version / spans），供 cache 与 manifest 共用。"""
        return {"names": list(self.names), "version": self.version, "spans": dict(self.spans)}

    def to_npz_payload(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """摊平成 `np.savez_compressed(**payload)` 可直接吃的形式。

        非数组字段统一进一个 JSON 串，避免 npz 里散落一堆 0 维对象数组。
        """
        payload: Dict[str, Any] = {
            "values": np.asarray(self.values, dtype=np.float32),
            "ts": np.asarray(self.ts, dtype=np.float64),
            "meta": np.array(
                json.dumps({**self.header(), "extra": dict(extra or {})}, ensure_ascii=False)
            ),
        }
        if self.valid is not None:
            payload["valid"] = np.asarray(self.valid)
        return payload

    @staticmethod
    def from_npz(data: Any) -> "tuple[FeatureBlock, Dict[str, Any]]":
        """`np.load(...)` 的结果 → (块, extra)。与 `to_npz_payload` 严格对称。"""
        meta = json.loads(str(data["meta"]))
        block = FeatureBlock(
            values=np.asarray(data["values"], dtype=np.float32),
            names=list(meta["names"]),
            ts=[float(v) for v in np.asarray(data["ts"], dtype=np.float64)],
            valid=np.asarray(data["valid"]) if "valid" in data.files else None,
            version=str(meta.get("version", "")),
            spans={k: list(v) for k, v in (meta.get("spans") or {}).items()},
        )
        return block, dict(meta.get("extra") or {})


def finite(values: np.ndarray) -> np.ndarray:
    """把 NaN/inf 兜底成 0，保持模型输入尺寸稳定且数值可送入 torch。"""
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
