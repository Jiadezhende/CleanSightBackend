"""CLEAN stage 离线模型策略。

本文件装三样东西：
    - **特征拼装纯函数**（`bbox_v3` / `bbox_v3_priors` / `bbox_v3_window_priors` /
      `bbox_v3_visual`）：把 blocks 层给的块拼成模型真正吃的那一块；
    - 三个 Segmenter 子类：一个 checkpoint 一个类，`needs` 声明要哪些块；
    - 模型输出到 SegmentFact 的解码逻辑。

基础 71 维 bbox 特征工程不在这里，在 `blocks/bbox.py`——那是所有模型共享的，
本文件只放**模型专属**的那几层叠加。

导出器与推理**调的是同一个 `build_input`**：`export --segmenter <类路径>` 拿到的字节，
就是该模型推理时实际吃的字节。单一真源不靠纪律，靠只有一条路径。

注意: 这里不包含训练流程。训练仍在独立 offline-model 仓内完成，后端只负责加载已训练
权重并执行离线推理。若未配置 model_path，CLEAN 模型会硬失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

from app.services.inference.models import SegmentFact
from app.services.inference.offline import blocks as blocks_api
from app.services.inference.offline.blocks import BlockKind
from app.services.inference.offline.blocks.bbox import effective_fps
from app.services.inference.offline.infer.impl import clean_nets
from app.services.inference.offline.infer.segmenter import OfflineSegmenter
from app.services.inference.offline.models import FeatureBlock, finite

ACTION_LABELS = [
    "idle",
    "long_brush_insert",
    "long_brush_withdraw",
    "short_brush_cleaning",
    "flush",
    "air_injection",
]


# ==================== 特征拼装（模块级纯函数，导出器与推理共用） ====================


def bbox_v3(blocks: Mapping[BlockKind, FeatureBlock]) -> FeatureBlock:
    """R0：bbox-only 基线（71 维）。

    blocks 层给的 bbox 块已经是这一份，此处即恒等——保留具名函数是为了让「吃哪套特征」
    在每个 Segmenter 上都是同一种写法。R0 是对照基准，后续 R1/R2 的增益相对它度量。
    """
    return blocks[BlockKind.BBOX]


def bbox_v3_priors(blocks: Mapping[BlockKind, FeatureBlock]) -> FeatureBlock:
    """R0 + 业务动作先验（73 维，ASFormer 用）。"""
    return add_business_priors(bbox_v3(blocks))


def bbox_v3_window_priors(blocks: Mapping[BlockKind, FeatureBlock]) -> FeatureBlock:
    """R0 + 多尺度居中滑窗均值 + 业务先验（151 维，BiGRU 用）。"""
    return add_business_priors(add_centered_window_stats(bbox_v3(blocks)))


def bbox_v3_visual(blocks: Mapping[BlockKind, FeatureBlock]) -> FeatureBlock:
    """R1：R0 + 全帧 CNN 深层全局池化向量（71 + C + 1 维）。回答「视觉信息到底有没有用」。

    **R1a / R1b 是同一套特征，只差 backbone**（Segmenter 类属性 `backbone` 填 `yolo` 或
    `resnet18`）：前者特征域与本场景匹配但与 bbox 同源，其增量只是「检测头丢掉的信息」；
    后者是一条独立的视觉通道。两者差异正是 R1 要测的核心变量。

    取不到像素的帧：视觉列置零，**语义由末列 `visual_valid` 承载**——零值本身不表达
    「画面里什么都没有」，模型据它判断（不变式 F4）。

    视觉向量保持 backbone 原始尺度不做归一化——归一化统计量属训练侧，落在这里会与训练仓
    的 normalizer 形成两份真源。
    """
    base = bbox_v3(blocks)
    vis = blocks.get(BlockKind.VGLOBAL)
    if vis is None:
        raise ValueError("bbox_v3_visual 需要 vglobal 块，请为该 Segmenter 配置 backbone")

    x = np.asarray(base.values, dtype=np.float32)
    if x.shape[0] != vis.frame_count:
        raise ValueError(f"视觉特征与 bbox 特征帧数不一致: {vis.frame_count} vs {x.shape[0]}")

    valid = (
        np.ones(x.shape[0], dtype=np.float32) if vis.valid is None
        else np.asarray(vis.valid, dtype=np.float32)
    )
    g = np.asarray(vis.values, dtype=np.float32) * valid[:, None]

    nb, nv = x.shape[1], g.shape[1]
    names = list(base.names) + list(vis.names) + ["visual_valid"]
    return base.replace_values(
        np.concatenate([x, g, valid[:, None]], axis=1),
        names,
        f"{base.version}+{vis.version}",
        spans={
            "bbox": [0, nb],
            "visual_global": [nb, nb + nv],
            "visual_valid": [nb + nv, nb + nv + 1],
        },
    )


def _centered_mean(values: np.ndarray, radius: int) -> np.ndarray:
    """以每帧为中心、半径 radius 的居中滑窗均值（边界收缩窗口，不补零）。"""
    if radius <= 0:
        return values.astype(np.float32)
    out = np.zeros_like(values, dtype=np.float32)
    for idx in range(len(values)):
        lo = max(0, idx - radius)
        hi = min(len(values), idx + radius + 1)
        out[idx] = values[lo:hi].mean(axis=0)
    return out


def add_centered_window_stats(
    block: FeatureBlock, windows: Tuple[int, ...] = (5, 15)
) -> FeatureBlock:
    """对 present/conf/speed 等列追加多尺度居中滑窗均值。

    居中滑窗即**看未来**——离线才允许，在线因果链路不可用此特征。
    """
    feature = np.asarray(block.values, dtype=np.float32)
    names = list(block.names)
    selected = [
        idx for idx, name in enumerate(names)
        if name.endswith(("_present", "_conf", "_speed", "_dist", "_delta", "_missing_age", "_imputed"))
    ]
    if not selected:
        return block

    base = feature[:, selected]
    extra_blocks: List[np.ndarray] = []
    extra_names: List[str] = []
    for window in windows:
        radius = max(1, window // 2)
        extra_blocks.append(_centered_mean(base, radius))
        extra_names.extend([f"{names[idx]}_center_mean_w{window}" for idx in selected])
    return block.replace_values(
        np.concatenate([feature, *extra_blocks], axis=1).astype(np.float32),
        names + extra_names,
        f"{block.version}+center_window",
    )


def _col(features: np.ndarray, name_to_idx: Dict[str, int], name: str) -> np.ndarray:
    """按列名取一整列；列不存在则返回全零（缺列→补零的统一不变式）。"""
    idx = name_to_idx.get(name)
    if idx is None:
        return np.zeros(features.shape[0], dtype=np.float32)
    return features[:, idx].astype(np.float32)


def _near_score(dist: np.ndarray) -> np.ndarray:
    """把归一化距离翻成 [0,1] 的接近度（越近越接近 1）。"""
    return np.clip(1.0 - dist, 0.0, 1.0).astype(np.float32)


def add_business_priors(block: FeatureBlock) -> FeatureBlock:
    """按业务规则叠加动作先验（接近度×存在×运动等）。

    只保留**两端目标都真能检出**的先验：灌注（syringe 稳定贴近先端）与注气（air_gun 同理）。
    原先另有 6 维长/短刷先验，全部建立在刷具检测框上——刷具检不出（见 blocks/bbox.py 的
    OBJECTS 注释），那 6 维恒为零，是纯噪声维度。长/短刷动作改由手部局部画面与跨帧变化
    承担，不在此处造先验。
    """
    x = np.asarray(block.values, dtype=np.float32)
    names = list(block.names)
    n = {name: idx for idx, name in enumerate(names)}

    hand = np.maximum(_col(x, n, "hand_top1_present"), _col(x, n, "hand_top2_present"))
    syringe = _col(x, n, "syringe_present")
    air_gun = _col(x, n, "air_gun_present")

    syringe_near = _near_score(_col(x, n, "syringe_to_scope_distal_end_dist"))
    air_near = _near_score(_col(x, n, "air_gun_to_scope_distal_end_dist"))

    syringe_stable = syringe * syringe_near * (1.0 - np.clip(_col(x, n, "syringe_speed"), 0.0, 1.0))
    air_stable = air_gun * air_near * (1.0 - np.clip(_col(x, n, "air_gun_speed"), 0.0, 1.0))

    priors = np.stack([hand * syringe_stable, hand * air_stable], axis=1).astype(np.float32)
    return block.replace_values(
        np.concatenate([x, priors], axis=1).astype(np.float32),
        names + ["prior_flush_stable", "prior_air_stable"],
        f"{block.version}+business_priors",
    )


# ==================== Segmenter ====================


class _CleanTorchSegmenter(OfflineSegmenter):
    """clean 模型策略基类：取块 → 拼输入 → torch 推理 → 解码 SegmentFact。

    子类声明三件事：`needs`（要哪些块）、`build_input`（怎么拼）、`_build_model`（什么网络）。
    三者由 checkpoint 绑死，故是**类的属性而非配置项**——换模型就换 Segmenter，
    配置里只有 `class:` 一个旋钮。
    """

    model_version = "clean_model_v1"
    feature_method = "v3"

    # 要哪些块。R1 子类改成 (BBOX, VGLOBAL) 并填 backbone。
    needs: Tuple[BlockKind, ...] = (BlockKind.BBOX,)
    backbone: str | None = None

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        model_path: str | None = None,
        min_duration_s: float = 0.2,
        device: str = "cpu",
        storage_dir: str | Path | None = None,
        offline_dir: str | Path | None = None,
    ):
        super().__init__(name, subscribes)
        self.model_path = model_path
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.device = device
        self.storage_dir = Path(storage_dir) if storage_dir else None
        self.offline_dir = Path(offline_dir) if offline_dir else None
        self._model = None
        self._normalizer: Tuple[Any, Any] | None = None
        self._last_result: dict | None = None

    # ---- 特征 ----

    def build_input(self, blocks: Mapping[BlockKind, FeatureBlock]) -> FeatureBlock:
        """把块拼成模型输入。子类一行覆盖即可换特征方案。

        导出器也调这个方法（不加载权重），因此训练样例与推理输入不可能漂。
        """
        return bbox_v3(blocks)

    def load_blocks(self, task_id: int, step_id: int) -> Dict[BlockKind, FeatureBlock]:
        """按 `needs` 取块。无特征时 `blocks.load` 抛 NoFeatures，由 cli 翻译成 skipped。"""
        return {
            kind: blocks_api.load(
                kind, task_id, step_id,
                sources=self.subscribes,
                backbone=self.backbone,
                device=self.device,
                storage_dir=self.storage_dir,
                offline_dir=self.offline_dir,
            )
            for kind in self.needs
        }

    # ---- 推理 ----

    def segment(self, task_id: int, step_id: int) -> List[SegmentFact]:
        """跑模型得到逐帧标签，解码成 SegmentFact；未配 model_path 硬失败，不做规则降级。"""
        model_input = self.build_input(self.load_blocks(task_id, step_id))
        if model_input.frame_count == 0:
            return []

        if not self.model_path:
            raise ValueError(
                f"{type(self).__name__} 未配置 model_path；CLEAN 离线模型不做规则降级"
            )

        labels, confs = self._predict(model_input)
        segments = self._labels_to_segments(model_input.ts, labels, confs)
        self._last_result = {
            "model_version": self.model_version,
            "model_class": type(self).__name__,
            "feature_method": self.feature_method,
            "feature_version": model_input.version,
            "feature_dim": model_input.feature_dim,
            "frame_count": model_input.frame_count,
            "backbone": self.backbone or "none",
            "ckpt": Path(str(self.model_path)).name,
            "frame_predictions": [
                {"ts": ts, "label": ACTION_LABELS[label], "conf": round(float(conf), 5)}
                for ts, label, conf in zip(model_input.ts, labels, confs)
            ],
            "segments": [s.to_json() for s in segments],
        }
        return segments

    def debug_result(self) -> dict | None:
        """返回最近一次 segment() 的逐帧预测/分段调试快照（未跑过为 None）。"""
        return self._last_result

    def _predict(self, model_input: FeatureBlock) -> Tuple[List[int], List[float]]:
        """惰性加载权重，归一化 + finite 兜底后前向，返回逐帧 (argmax 标签, 置信度)。"""
        import torch

        if self._model is None:
            self._load_model(model_input, len(ACTION_LABELS))

        x_np = np.asarray(model_input.values, dtype=np.float32)
        if self._normalizer is not None:
            mean, std = self._normalizer
            x_np = (x_np - mean) / std
        x_np = finite(x_np)
        x = torch.tensor(x_np[None, :, :], dtype=torch.float32)
        self._model.eval()
        with torch.no_grad():
            logits = self._model(x)[0].transpose(0, 1)
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
        return probs.argmax(axis=1).astype("int64").tolist(), probs.max(axis=1).astype("float32").tolist()

    def _load_model(self, model_input: FeatureBlock, class_count: int) -> None:
        """加载 .pt checkpoint 并校验 feature_names/feature_version 与后端输入一致，取出 normalizer。"""
        import torch

        path = Path(str(self.model_path))
        if not path.exists():
            raise FileNotFoundError(f"clean 离线模型权重不存在: {path}")

        try:
            # PyTorch 2.6 起 torch.load 默认 weights_only=True，会拒绝包含 numpy
            # normalizer 的可信训练 checkpoint；这里加载的是本地 offline-model 产物。
            checkpoint = torch.load(path, map_location="cpu", weights_only=False)
        except TypeError:
            checkpoint = torch.load(path, map_location="cpu")
        self._model = self._build_model(model_input.feature_dim, class_count)
        self._model.load_state_dict(checkpoint.get("state_dict", checkpoint), strict=True)

        # 特征列逐项比对：这是防 train/serve skew 的实际门禁，不是装饰
        feature_names = checkpoint.get("feature_names")
        if feature_names is not None and list(feature_names) != list(model_input.names):
            raise ValueError("clean 离线模型 feature_names 与后端特征列不一致")
        feature_version = checkpoint.get("feature_version")
        if isinstance(feature_version, str):
            ckpt_version = feature_version
        elif feature_version is not None and len(feature_version):
            ckpt_version = str(feature_version[0])
        else:
            ckpt_version = None
        if ckpt_version is not None and ckpt_version != model_input.version:
            raise ValueError(
                f"clean 离线模型 feature_version 不一致: checkpoint={ckpt_version}, "
                f"input={model_input.version}"
            )

        mean = checkpoint.get("normalizer_mean")
        std = checkpoint.get("normalizer_std")
        if mean is not None and std is not None:
            self._normalizer = (mean, std)

    def _labels_to_segments(
        self, timestamps: Sequence[float], labels: Sequence[int], confs: Sequence[float]
    ) -> List[SegmentFact]:
        """把逐帧标签合并成连续动作段（跳过 idle、过滤短于 min_duration_s 的段）。"""
        segments: List[SegmentFact] = []
        cur_label: int | None = None
        cur_start = cur_end = 0.0
        cur_conf = 0.0
        cur_count = 0

        def flush() -> None:
            nonlocal cur_label, cur_conf, cur_count
            if cur_label is not None and cur_label != 0 and (cur_end - cur_start) >= self.min_duration_s:
                segments.append(SegmentFact(
                    source=self.name,
                    label=ACTION_LABELS[cur_label],
                    start=round(cur_start, 6),
                    end=round(cur_end, 6),
                    conf=min(1.0, max(0.0, cur_conf / max(cur_count, 1))),
                    meta={"model_version": self.model_version},
                ))
            cur_label = None
            cur_conf = 0.0
            cur_count = 0

        for ts, label, conf in zip(timestamps, labels, confs):
            label = int(label)
            if label == 0:
                flush()
                continue
            if cur_label != label:
                flush()
                cur_label = label
                cur_start = cur_end = float(ts)
                cur_conf = float(conf)
                cur_count = 1
            else:
                cur_end = float(ts)
                cur_conf += float(conf)
                cur_count += 1
        flush()
        return segments

    def _build_model(self, in_dim: int, class_count: int):
        """构建本模型的 torch 网络（子类按自身结构实现）。"""
        raise NotImplementedError

    # ---- 供导出器使用的元信息 ----

    def input_fps(self, model_input: FeatureBlock) -> float:
        """模型输入的真实采样率（从 ts 反推，不存字段避免两份真源）。"""
        return effective_fps(model_input.ts)


class CleanMSTCNBiLSTMSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 MS-TCN + BiLSTM 离线模型。吃基础 v3（71 维）。"""

    model_version = "clean_mstcn_bilstm_v1"
    feature_method = "v3"

    def _build_model(self, in_dim: int, class_count: int):
        return clean_nets.make_mstcn_bilstm(in_dim, class_count)


class CleanASFormerSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 ASFormer 风格离线模型。吃 v3 + business_priors（73 维）。"""

    model_version = "clean_asformer_v1"
    feature_method = "business_priors"

    def build_input(self, blocks: Mapping[BlockKind, FeatureBlock]) -> FeatureBlock:
        return bbox_v3_priors(blocks)

    def _build_model(self, in_dim: int, class_count: int):
        return clean_nets.make_asformer(in_dim, class_count)


class CleanBiGRUSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 BiGRU 离线模型。吃 v3 + center_window + business_priors（151 维）。"""

    model_version = "clean_bigru_v1"
    feature_method = "window_stats+business_priors"

    def build_input(self, blocks: Mapping[BlockKind, FeatureBlock]) -> FeatureBlock:
        return bbox_v3_window_priors(blocks)

    def _build_model(self, in_dim: int, class_count: int):
        return clean_nets.make_bigru(in_dim, class_count)
