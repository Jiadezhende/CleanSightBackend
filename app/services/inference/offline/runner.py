"""离线分割编排层 —— 把 (task_id, step_id) 一次跑通 FeatureStore → 策略 → FactLedger。

调用方（CLI / 测试）显式给 `(task_id, step_id[, strategy])`，Runner：
    1. 按 step_id 取 stage 配置，实例化 offline 策略（未启用则 skip）；
    2. 一次扫 FeatureStore 读订阅 source 的完整序列；
    3. 准入闸：策略报内存成本，超单进程预算则 skip（不覆盖旧事实）；
    4. 策略 preprocess → segment 产出 SegmentFact；
    5. 校验 + 补 producer + 排序，幂等 replace 写 FactLedger。

离线链路只识别稳定存储键 `(task_id, step_id)`；不接 client / CQ / 在线 Operator / 告警 / DB。
Runner 自建绑定 `settings.storage_base_dir` 的 FeatureStore / FactLedger（不复用在线单例——本就独立进程）。
调用方须保证输入已封口（step 已停写、缓冲已 flush）；Runner 不证明在线写入已结束。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

from app.services.inference.config import InferenceConfig, load_stage_config
from app.services.inference.feature.store import FactLedger, FeatureStore
from app.services.inference.types import SegmentFact
from app.services.inference.stage_factory import StageFactory
from app.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfflineRunSpec:
    """一次离线运行的输入：稳定存储键 + 可选策略覆盖。"""

    task_id: int
    step_id: int
    strategy: Optional[str] = None  # 覆盖 stage.offline.class（全限定路径），开发期对比策略用


@dataclass(frozen=True)
class OfflineRunResult:
    """一次离线运行的结果。status ∈ {completed, skipped}；异常经 run() 抛出，不落此结构。"""

    status: str
    producer: Optional[str]
    segment_count: int
    message: str = ""


class OfflineRunner:
    """离线分割 Runner（同步、单次、独立进程内运行）。"""

    def __init__(
        self,
        base_dir: Optional[Union[str, Path]] = None,
        config_path: Optional[Path] = None,
        config: Optional[InferenceConfig] = None,
        memory_budget_mb: Optional[int] = None,
    ):
        base = Path(base_dir) if base_dir is not None else settings.storage_base_dir
        self._base_dir = Path(base)
        self._feature_store = FeatureStore(base)
        self._fact_ledger = FactLedger(base)
        self._config_path = config_path
        self._config = config  # 显式注入优先（测试用）；否则走 load_stage_config 单例
        # 准入预算：None 时取通用的单进程 RAM 上限（settings.process_memory_budget_mb）。
        self._memory_budget_mb = (
            int(memory_budget_mb) if memory_budget_mb is not None
            else int(settings.process_memory_budget_mb)
        )

    def run(self, spec: OfflineRunSpec) -> OfflineRunResult:
        config = self._config if self._config is not None else load_stage_config(self._config_path)

        # 存储 step_id（数字）与 stage 配置 key 正交：数字命中即恒等，未知回退 MOCK（与在线同源）。
        # 存储读写始终用原 spec.step_id，不用 stage_key。
        stage_key = config.resolve_stage(spec.step_id)
        if config.get_stage_config(stage_key) is None:
            return OfflineRunResult("skipped", None, 0, f"未知 stage '{stage_key}'")

        factory = StageFactory(config)
        segmenter = factory.create_offline_segmenter(stage_key, override_class=spec.strategy)
        if segmenter is None:
            return OfflineRunResult("skipped", None, 0, f"stage '{stage_key}' offline 未启用")

        producer = segmenter.name
        frames = self._feature_store.load(spec.task_id, spec.step_id)
        present = set().union(*(ff.by_source.keys() for ff in frames)) if frames else set()
        empty = [s for s in segmenter.subscribes if s not in present]
        if empty:
            # 任一订阅 source 无数据：跳过，不覆盖旧事实
            return OfflineRunResult(
                "skipped", producer, 0, f"订阅 source 无特征: {empty}"
            )

        over_budget = self._check_memory_budget(segmenter, frames)
        if over_budget is not None:
            # 超预算：跳过，不覆盖旧事实（与"订阅 source 无数据"同口径）
            logger.warning("[OfflineRunner] %s task=%s step=%s", over_budget, spec.task_id, spec.step_id)
            return OfflineRunResult("skipped", producer, 0, over_budget)

        model_input = segmenter.preprocess(frames)
        facts = segmenter.segment(model_input)  # 算法异常向上抛出，不写

        validated = self._validate_and_stamp(facts, producer)
        validated.sort(key=lambda f: (f.start, f.end, f.label))

        self._fact_ledger.replace_segments(
            spec.task_id, spec.step_id, producer, validated
        )
        self._maybe_write_debug(spec, segmenter)
        logger.info(
            "[OfflineRunner] completed task=%s step=%s producer=%s segments=%d",
            spec.task_id, spec.step_id, producer, len(validated),
        )
        return OfflineRunResult("completed", producer, len(validated))

    def _check_memory_budget(self, segmenter, frames) -> Optional[str]:
        """准入闸：策略报成本、框架判预算。超预算返回说明文案，否则 None。

        T 在这条链路上没有任何上限（FeatureStore 无行数上限、step 时长由外部切分决定），
        所以"能跑多长"必须在动手前判一次，而不是等 OOM。判在 preprocess **之前**——
        preprocess 本身就是最大的那笔开销，判在它之后等于没判。

        策略不报成本（返回 None）就放行：轻量/规则型策略无需为此写一份估算。
        估算异常同样放行——闸门失准不该让本可跑通的任务失败，Linux 侧还有 RLIMIT_AS 兜底（见 cli.py）。
        """
        try:
            estimate_mb = segmenter.estimate_memory_mb(frames)
        except Exception as e:
            logger.warning("[OfflineRunner] 内存估算失败，跳过预算闸: %s", e)
            return None
        if estimate_mb is None or estimate_mb <= self._memory_budget_mb:
            return None
        detections = sum(len(fd.detections) for ff in frames for fd in ff.by_source.values())
        return (
            f"超内存预算: 估算 {estimate_mb:.0f} MB > 预算 {self._memory_budget_mb} MB"
            f"（frames={len(frames)} detections={detections}）；"
            f"缩短 step 时长，或调高 settings.process_memory_budget_mb / CLI --memory-budget-mb"
        )

    def _maybe_write_debug(self, spec: OfflineRunSpec, segmenter) -> None:
        """策略若产逐帧调试产物（debug_result 非 None），落一份 offline_inference_result.json。

        与 facts.jsonl 同目录，供调试/对比；写失败只告警不影响已成功的事实落盘。
        """
        debug = segmenter.debug_result()
        if debug is None:
            return
        path = self._base_dir / str(spec.task_id) / str(spec.step_id) / "offline_inference_result.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"task_id": spec.task_id, "step_id": spec.step_id, **debug}
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("[OfflineRunner] 逐帧调试 JSON 落盘失败 %s: %s", path, e)

    @staticmethod
    def _validate_and_stamp(facts: List[SegmentFact], producer: str) -> List[SegmentFact]:
        """全量校验 SegmentFact 并补 meta.producer；任一非法整批失败（不部分写）。"""
        for f in facts:
            if not isinstance(f, SegmentFact):
                raise ValueError(f"segmenter 产出非 SegmentFact: {type(f).__name__}")
            if f.source != producer:
                raise ValueError(
                    f"SegmentFact.source '{f.source}' != segmenter name '{producer}'"
                )
            if not (math.isfinite(f.start) and math.isfinite(f.end)):
                raise ValueError(f"SegmentFact 时间非有限数: start={f.start} end={f.end}")
            if f.start > f.end:
                raise ValueError(f"SegmentFact start > end: {f.start} > {f.end}")
            if not (0.0 <= f.conf <= 1.0):
                raise ValueError(f"SegmentFact conf 越界: {f.conf}")
            existing = f.meta.get("producer")
            if existing is not None and existing != producer:
                raise ValueError(
                    f"SegmentFact.meta.producer 冲突: '{existing}' != '{producer}'"
                )
            f.meta["producer"] = producer
        return facts
