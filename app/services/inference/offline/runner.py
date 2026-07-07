"""离线分割编排层 —— 把 (task_id, step_id) 一次跑通 FeatureStore → 策略 → FactLedger。

调用方（CLI / 测试）显式给 `(task_id, step_id[, strategy])`，Runner：
    1. 按 step_id 取 stage 配置，实例化 offline 策略（未启用则 skip）；
    2. 一次扫 FeatureStore 读订阅 source 的完整序列；
    3. 策略 preprocess → segment 产出 SegmentFact；
    4. 校验 + 补 producer + 排序，幂等 replace 写 FactLedger。

离线链路只识别稳定存储键 `(task_id, step_id)`；不接 client / CQ / 在线 Operator / 告警 / DB。
Runner 自建绑定 `settings.storage_base_dir` 的 FeatureStore / FactLedger（不复用在线单例——本就独立进程）。
调用方须保证输入已封口（step 已停写、缓冲已 flush）；Runner 不证明在线写入已结束。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

from app.services.inference.config import InferenceConfig, load_stage_config
from app.services.inference.feature.store import FactLedger, FeatureStore
from app.services.inference.models import SegmentFact
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
    ):
        base = Path(base_dir) if base_dir is not None else settings.storage_base_dir
        self._feature_store = FeatureStore(base)
        self._fact_ledger = FactLedger(base)
        self._config_path = config_path
        self._config = config  # 显式注入优先（测试用）；否则走 load_stage_config 单例

    def run(self, spec: OfflineRunSpec) -> OfflineRunResult:
        stage_key = str(spec.step_id)
        config = self._config if self._config is not None else load_stage_config(self._config_path)

        if config.get_stage_config(stage_key) is None:
            return OfflineRunResult("skipped", None, 0, f"未知 stage '{stage_key}'")

        factory = StageFactory(config)
        segmenter = factory.create_offline_segmenter(stage_key, override_class=spec.strategy)
        if segmenter is None:
            return OfflineRunResult("skipped", None, 0, f"stage '{stage_key}' offline 未启用")

        producer = segmenter.name
        streams = self._feature_store.load_many(
            spec.task_id, spec.step_id, segmenter.subscribes
        )
        empty = [s for s in segmenter.subscribes if not streams.get(s)]
        if empty:
            # 任一订阅 source 无数据：跳过，不覆盖旧事实
            return OfflineRunResult(
                "skipped", producer, 0, f"订阅 source 无特征: {empty}"
            )

        model_input = segmenter.preprocess(streams)
        facts = segmenter.segment(model_input)  # 算法异常向上抛出，不写

        validated = self._validate_and_stamp(facts, producer)
        validated.sort(key=lambda f: (f.start, f.end, f.label))

        self._fact_ledger.replace_segments(
            spec.task_id, spec.step_id, producer, validated
        )
        logger.info(
            "[OfflineRunner] completed task=%s step=%s producer=%s segments=%d",
            spec.task_id, spec.step_id, producer, len(validated),
        )
        return OfflineRunResult("completed", producer, len(validated))

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
