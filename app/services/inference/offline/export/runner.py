"""导出编排层 —— 把 (task_id, step_id, recipe) 一次跑成一份模型输入样例。

    FeatureStore.load(task_id, step_id) → List[FrameFeature]
        │
        ▼ [可选] 帧源取像素 + backbone 前向 → VisualFrames   ← 第 3 阶接入，本阶恒 None
        │
        ▼ recipe(frames, visual) → ModelInput               ← importlib 按全限定路径取
        │
        ▼ input.npz + manifest.json

定位：**可行性验证工具**，产出不同方案的模型输入样例，回答「哪个 recipe 值得继续」。
具体训练流程不在本仓库，故这里不建数据集版本治理、不做批量流水线。

与离线分割 Runner（../runner.py）的关系：同吃 `(task_id, step_id)` 稳定存储键、同不接
client/CQ/DB，但那条产 SegmentFact 写 FactLedger，这条产模型输入写导出目录——两条独立管线，
刻意不复用彼此的 Runner。
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import numpy as np

from app.domain.detection import FrameFeature
from app.services.inference.feature.store import FeatureStore
from app.services.inference.offline.export.models import (
    ExportQuality,
    ExportResult,
    ExportSpec,
    VisualFrames,
)
from app.settings import settings

logger = logging.getLogger(__name__)

# 导出根目录：由 storage_base_dir 派生的固定子目录，**不新增配置项**（导出器是手动跑的
# 实验工具，CLI --out-dir 就是它的旋钮）。与 ClipBuilder 默认 `{base_dir}/.lab_exports` 同款约定。
#
# 关键：产物**不能**落在 `{base}/{task_id}/{step_id}/`——那里受 cleanup_days（默认 7 天）
# TTL 回收（StorageCleanupWorker 按 `{base}/*/*/metadata.json` 判定过期 step 目录并 rmtree
# 整个目录）。同理产物 manifest 绝不可命名为 metadata.json，否则导出目录会被当成 step 目录删掉。
_EXPORT_SUBDIR = ".offline_exports"

_INPUT_FILENAME = "input.npz"
_MANIFEST_FILENAME = "manifest.json"  # ← 不叫 metadata.json，见上


def _load_recipe(path: str) -> Callable[..., Any]:
    """按全限定路径取 recipe 函数（与 StageFactory 取 offline.class 同款约定）。

    加载失败一律 **fail-fast**（抛异常，不静默跳过）——导出跑空比报错更难查。
    """
    if "." not in path:
        raise ValueError(f"recipe 须为全限定路径，收到: {path!r}")
    module_path, _, attr = path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ValueError(f"recipe 模块加载失败: {module_path}") from e
    try:
        fn = getattr(module, attr)
    except AttributeError as e:
        raise ValueError(f"recipe 模块 {module_path} 内无 {attr!r}") from e
    if not callable(fn):
        raise ValueError(f"recipe {path} 不可调用")
    return fn


def recipe_short_name(path: str) -> str:
    """全限定路径 → 目录名用的短名（`...clean.export_r0` → `r0`）。"""
    tail = path.rpartition(".")[2]
    return tail[len("export_"):] if tail.startswith("export_") else tail


class ExportRunner:
    """离线特征导出 Runner（同步、单次、独立进程内运行）。"""

    def __init__(self, base_dir: Optional[Path] = None):
        base = Path(base_dir) if base_dir is not None else settings.storage_base_dir
        self._base_dir = Path(base)
        self._feature_store = FeatureStore(base)

    def default_out_dir(self, spec: ExportSpec) -> Path:
        """产物目录：`{base}/.offline_exports/{task}/{step}/{recipe}@{backbone}/`。"""
        tag = f"{recipe_short_name(spec.recipe)}@{spec.backbone or 'none'}"
        return self._base_dir / _EXPORT_SUBDIR / str(spec.task_id) / str(spec.step_id) / tag

    def run(self, spec: ExportSpec) -> ExportResult:
        """跑一次导出。特征为空 → skipped 且不写产物；recipe 加载/执行失败 → 抛出。"""
        recipe = _load_recipe(spec.recipe)  # 先加载再读特征：配置错就别白读一遍盘

        frames = self._feature_store.load(spec.task_id, spec.step_id)
        if not frames:
            return ExportResult(
                "skipped", spec.recipe, message="无特征（features.jsonl 缺失或为空）"
            )

        visual = self._build_visual(spec, frames)
        model_input = recipe(frames, visual)

        out_dir = Path(spec.out_dir) if spec.out_dir is not None else self.default_out_dir(spec)
        quality = ExportQuality(
            frames_total=len(frames),
            needs_pixels=visual is not None,
            pixel_hit=int(np.count_nonzero(visual.valid)) if visual is not None and visual.valid is not None else 0,
        )
        self._write(out_dir, spec, model_input, quality)

        logger.info(
            "[ExportRunner] completed task=%s step=%s recipe=%s frames=%d dim=%d → %s",
            spec.task_id, spec.step_id, spec.recipe,
            model_input.frame_count, model_input.feature_dim, out_dir,
        )
        return ExportResult(
            "completed",
            spec.recipe,
            frame_count=model_input.frame_count,
            feature_dim=model_input.feature_dim,
            out_dir=out_dir,
            quality=quality,
        )

    def _build_visual(
        self, spec: ExportSpec, frames: Sequence[FrameFeature]
    ) -> Optional[VisualFrames]:
        """取像素 + 跑 backbone → VisualFrames。

        本阶（R0）恒返回 None：R0 不消费像素，帧源与 backbone 留到第 3 阶（R1a/R1b）接入。
        届时本方法按 spec.backbone 决定是否解码，其余流程一行不改。
        """
        if spec.backbone is None:
            return None
        raise NotImplementedError(
            "视觉特征（帧源 + backbone）尚未接入，当前只支持不消费像素的 recipe；"
            "请去掉 --backbone 或等待 R1a/R1b 落地"
        )

    def _write(
        self, out_dir: Path, spec: ExportSpec, model_input: Any, quality: ExportQuality
    ) -> None:
        """落 input.npz + manifest.json（覆盖写：同 spec 重跑即覆盖，无需清理旧产物）。"""
        out_dir.mkdir(parents=True, exist_ok=True)

        np.savez_compressed(
            out_dir / _INPUT_FILENAME,
            features=np.asarray(model_input.features, dtype=np.float32),
            timestamps=np.asarray(model_input.timestamps, dtype=np.float64),
        )

        manifest = {
            "task_id": spec.task_id,
            "step_id": spec.step_id,
            "recipe": spec.recipe,
            "backbone": spec.backbone or "none",
            "device": spec.device,
            "feature_version": model_input.feature_version,
            "feature_dim": model_input.feature_dim,
            "feature_names": list(model_input.feature_names),
            "frame_count": model_input.frame_count,
            "fps": model_input.fps,
            "ts_start": model_input.timestamps[0] if model_input.timestamps else None,
            "ts_end": model_input.timestamps[-1] if model_input.timestamps else None,
            "quality": quality.to_json(),
        }
        (out_dir / _MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
