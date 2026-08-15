"""离线编排层 —— 把 (task_id, step_id) 一次跑成事实或训练样例。

    stage 配置 → StageFactory 造 Segmenter
        │
        ▼ segmenter.segment(task, step)        ← 取块 / 拼输入 / 前向全在策略内部
        ▼ 校验 + 补 producer + 排序
        ▼ FactLedger.replace_segments(...)     ← 契约不变，按 producer 幂等替换

**编排层不认识特征块**：这里没有 BlockKind、没有 blocks.load、也不碰 FeatureStore。
块的加载、拼装、缓存全在 Segmenter 与 `blocks/` 内部。唯一的例外是 `NoFeatures`——
它是「无输入」这一编排级结论的载体，要翻译成 skipped。

`export` 与 `infer` 走**同一个 Segmenter 类的 `build_input`**：导出的字节就是该模型推理时
实际吃的字节。训练样例与线上输入不可能漂——不是靠纪律，是只有一条路径。

离线链路只识别稳定存储键 `(task_id, step_id)`；不接 client / CQ / 在线 Operator / 告警 / DB。
调用方须保证输入已封口（step 已停写、缓冲已 flush）；本层不证明在线写入已结束。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    """一次离线执行的结果。status ∈ {completed, skipped}；异常经调用处捕获，不落此结构。"""

    status: str
    producer: Optional[str]
    segment_count: int = 0
    message: str = ""


def _build_segmenter(args: argparse.Namespace, storage_dir=None, offline_dir=None):
    """按 stage 配置造 Segmenter；未启用返回 None。

    存储 step_id（数字）与 stage 配置 key 正交：数字命中即恒等，未知回退 MOCK（与在线同源）。
    存储读写始终用原 step_id，不用 stage_key。
    """
    from app.services.inference.config import load_stage_config
    from app.services.inference.stage_factory import StageFactory

    config = getattr(args, "config", None) or load_stage_config(getattr(args, "config_path", None))
    stage_key = config.resolve_stage(args.step_id)
    if config.get_stage_config(stage_key) is None:
        return None, f"未知 stage '{stage_key}'"

    segmenter = StageFactory(config).create_offline_segmenter(
        stage_key, override_class=getattr(args, "segmenter", None)
    )
    if segmenter is None:
        return None, f"stage '{stage_key}' offline 未启用"
    for attr, value in (("storage_dir", storage_dir), ("offline_dir", offline_dir)):
        if value is not None and hasattr(segmenter, attr):
            setattr(segmenter, attr, Path(value))
    return segmenter, ""


def run_infer(args: argparse.Namespace, storage_dir=None, offline_dir=None) -> RunResult:
    """读特征块 → 跑策略 → 校验 → 幂等写 FactLedger。"""
    from app.services.inference.feature.store import FactLedger
    from app.services.inference.offline.blocks import NoFeatures
    from app.settings import settings

    segmenter, why = _build_segmenter(args, storage_dir, offline_dir)
    if segmenter is None:
        return RunResult("skipped", None, 0, why)

    producer = segmenter.name
    try:
        facts = segmenter.segment(args.task_id, args.step_id)
    except NoFeatures as e:
        # 无输入 ≠ 空结果：跳过且**不覆盖旧事实**
        return RunResult("skipped", producer, 0, str(e))

    validated = _validate_and_stamp(facts, producer)
    validated.sort(key=lambda f: (f.start, f.end, f.label))

    base = Path(storage_dir) if storage_dir is not None else settings.storage_base_dir
    FactLedger(base).replace_segments(args.task_id, args.step_id, producer, validated)
    _write_debug(args, segmenter, offline_dir)
    logger.info(
        "[offline] completed task=%s step=%s producer=%s segments=%d",
        args.task_id, args.step_id, producer, len(validated),
    )
    return RunResult("completed", producer, len(validated))


def _validate_and_stamp(facts: List, producer: str) -> List:
    """全量校验 SegmentFact 并补 meta.producer；任一非法整批失败（不部分写）。"""
    from app.services.inference.models import SegmentFact

    for f in facts:
        if not isinstance(f, SegmentFact):
            raise ValueError(f"segmenter 产出非 SegmentFact: {type(f).__name__}")
        if f.source != producer:
            raise ValueError(f"SegmentFact.source '{f.source}' != segmenter name '{producer}'")
        if not (math.isfinite(f.start) and math.isfinite(f.end)):
            raise ValueError(f"SegmentFact 时间非有限数: start={f.start} end={f.end}")
        if f.start > f.end:
            raise ValueError(f"SegmentFact start > end: {f.start} > {f.end}")
        if not (0.0 <= f.conf <= 1.0):
            raise ValueError(f"SegmentFact conf 越界: {f.conf}")
        existing = f.meta.get("producer")
        if existing is not None and existing != producer:
            raise ValueError(f"SegmentFact.meta.producer 冲突: '{existing}' != '{producer}'")
        f.meta["producer"] = producer
    return facts


def _write_debug(args: argparse.Namespace, segmenter, offline_dir) -> None:
    """策略若产逐帧调试产物，落一份到 `.cache`。

    **落 .cache 不落 step 目录**：逐帧预测是可重建的调试产物，不是正式结果（正式结果是
    FactLedger 里的 SegmentFact）。早先它写在 `{storage}/{task}/{step}/` 下，反而受
    cleanup_days 的 7 天 TTL，跑完一周就蒸发，且看不出是哪份特征、哪个 backbone、哪个
    ckpt 产的。写失败只告警，不影响已成功的事实落盘。
    """
    from app.services.inference.offline.blocks import cache

    debug = segmenter.debug_result()
    if debug is None:
        return
    tag = f"{type(segmenter).__name__}"
    path = cache.cache_dir(args.task_id, args.step_id, offline_dir) / f"infer_{tag}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"task_id": args.task_id, "step_id": args.step_id, **debug}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[offline] 逐帧调试产物落盘失败 %s: %s", path, e)


def run_export(args: argparse.Namespace, storage_dir=None, offline_dir=None) -> RunResult:
    """取块 → Segmenter.build_input → 落训练样例（npz + manifest），**不前向**。"""
    import numpy as np

    from app.services.inference.offline.blocks import NoFeatures, cache
    from app.services.inference.offline.blocks.bbox import effective_fps
    from app.services.inference.offline.blocks.frame_source import FetchStats

    segmenter, why = _build_segmenter(args, storage_dir, offline_dir)
    if segmenter is None:
        return RunResult("skipped", None, 0, why)

    stats = FetchStats()
    try:
        blocks = _load_blocks_for_export(segmenter, args, stats)
    except NoFeatures as e:
        return RunResult("skipped", segmenter.name, 0, str(e))
    model_input = segmenter.build_input(blocks)

    tag = type(segmenter).__name__
    out_dir = Path(args.out_dir) if args.out_dir else cache.cache_dir(
        args.task_id, args.step_id, offline_dir
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_dir / f"input_{tag}.npz",
        features=np.asarray(model_input.values, dtype=np.float32),
        timestamps=np.asarray(model_input.ts, dtype=np.float64),
    )
    manifest = {
        "task_id": args.task_id,
        "step_id": args.step_id,
        "segmenter": f"{type(segmenter).__module__}.{tag}",
        "backbone": getattr(segmenter, "backbone", None) or "none",
        "device": args.device,
        "feature_version": model_input.version,
        "feature_dim": model_input.feature_dim,
        "feature_names": list(model_input.names),
        # 各特征块的列区间 {块名: [起, 止)}。训练仓据此把 bbox / 视觉 / valid 切开，
        # 各分支独立投影到等宽再融合（F1~F3 融合方式全都要分支独立编码）。
        "spans": dict(model_input.spans),
        "frame_count": model_input.frame_count,
        "fps": effective_fps(model_input.ts),
        "ts_start": model_input.ts[0] if model_input.ts else None,
        "ts_end": model_input.ts[-1] if model_input.ts else None,
        # 取帧质量直接用 FetchStats，不再重壳一份（早先的 ExportQuality 抄漏了 decode_short）
        "quality": {**vars(stats), "pixel_miss": stats.pixel_miss, "frames_total": model_input.frame_count},
    }
    # manifest 绝不叫 metadata.json —— 那会被 StorageCleanupWorker 当成 step 目录 rmtree
    (out_dir / f"manifest_{tag}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "[offline] export completed task=%s step=%s segmenter=%s frames=%d dim=%d → %s",
        args.task_id, args.step_id, tag, model_input.frame_count, model_input.feature_dim, out_dir,
    )
    return RunResult("completed", segmenter.name, model_input.frame_count, str(out_dir))


def _load_blocks_for_export(segmenter, args: argparse.Namespace, stats):
    """导出路径取块：与推理同一批 kind，额外把取帧统计带出来写进 manifest。"""
    from app.services.inference.offline import blocks as blocks_api

    return {
        kind: blocks_api.load(
            kind, args.task_id, args.step_id,
            sources=segmenter.subscribes,
            backbone=getattr(segmenter, "backbone", None),
            device=args.device,
            storage_dir=getattr(segmenter, "storage_dir", None),
            offline_dir=getattr(segmenter, "offline_dir", None),
            stats=stats,
        )
        for kind in segmenter.needs
    }


