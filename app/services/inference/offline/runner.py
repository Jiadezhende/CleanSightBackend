"""离线分割编排层 —— 把 (task_id, step_id) 一次跑通 detections.jsonl → 策略 → temporal.jsonl。

调用方（CLI / 测试）显式给 `(task_id, step_id[, strategy])`，Runner：
    1. 按 step_id 取 stage 配置，实例化 offline 策略（未启用则 skip）；
    2. 一次读该 step 的完整检测序列（为空则 skip）；
    3. 策略 preprocess → segment 产出 TemporalSegment（producer = 策略类名）；
    4. 校验 + 排序，**读回既有事实 → 删掉该 step 全部旧分段、保留 TemporalEvent → 整体写回**。

路由：`strict=False`（CLI 默认）未配 step 经 `resolve_stage` 回落 MOCK；`strict=True`（作业服务）
只认精确命中的 stage，未配 / offline 为空直接 skipped，不回落。

离线链路只识别稳定存储键 `(task_id, step_id)`；不接 client / CQ / 在线 Operator / 告警 / DB。
落盘全经 `app.storage.inference`（存储根归 `settings`，故本类不收 `base_dir`）。

**调用方须保证输入已封口**：step 已停写、且 recording 的 detections 队列已把缓冲排空
（在线链路是异步落盘的）。Runner 不证明这一点。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.domain.temporal import TemporalSegment
from app.services.inference.config import InferenceConfig, load_stage_config
from app.services.inference.stage_factory import StageFactory
from app.storage import inference as inference_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfflineRunSpec:
    """一次离线运行的输入：稳定存储键 + 可选策略覆盖。"""

    task_id: int
    step_id: int
    strategy: Optional[str] = None  # 覆盖 stage.offline.class（全限定路径），开发期对比策略用
    strict: bool = False  # True = 不回落 MOCK：未配 stage / offline 为空即 skipped


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
        config_path: Optional[Path] = None,
        config: Optional[InferenceConfig] = None,
    ):
        self._config_path = config_path
        self._config = config  # 显式注入优先（测试用）；否则走 load_stage_config 单例

    def run(self, spec: OfflineRunSpec) -> OfflineRunResult:
        config = self._config if self._config is not None else load_stage_config(self._config_path)

        # 存储 step_id（数字）与 stage 配置 key 正交：数字命中即恒等，未知回退 MOCK（与在线同源）；
        # strict 只认精确命中。存储读写始终用原 spec.step_id，不用 stage_key。
        stage_key = str(spec.step_id) if spec.strict else config.resolve_stage(spec.step_id)
        if config.get_stage_config(stage_key) is None:
            return OfflineRunResult("skipped", None, 0, f"未知 stage '{stage_key}'")

        factory = StageFactory(config)
        segmenter = factory.create_offline_segmenter(stage_key, override_class=spec.strategy)
        if segmenter is None:
            return OfflineRunResult("skipped", None, 0, f"stage '{stage_key}' offline 未启用")

        producer = segmenter.name
        frames = inference_store.read_detections(spec.task_id, spec.step_id)
        if not frames:
            # 无检测结果：跳过，不覆盖旧事实
            return OfflineRunResult("skipped", producer, 0, "该 step 无检测结果")

        model_input = segmenter.preprocess(frames)
        facts = segmenter.segment(model_input)  # 算法异常向上抛出，不写

        validated = self._validate(facts, producer)
        validated.sort(key=lambda f: (f.start, f.end, f.label))

        self._replace_segments(spec.task_id, spec.step_id, validated)
        self._maybe_write_debug(spec, segmenter)
        logger.info(
            "[OfflineRunner] completed task=%s step=%s producer=%s segments=%d",
            spec.task_id, spec.step_id, producer, len(validated),
        )
        return OfflineRunResult("completed", producer, len(validated))

    @staticmethod
    def _replace_segments(task_id: int, step_id: int, facts: List[TemporalSegment]) -> None:
        """幂等替换该 step 的分段：读回既有 → 丢掉全部旧 TemporalSegment、保留 TemporalEvent → 整体写回。

        一个 stage 至多一个离线模型，换模型重跑时旧模型（旧类名）的分段整体被替换，不与新结果并存。
        `write_temporal` 是**整体替换**，盲写会吃掉在线产出的 `TemporalEvent`，故合并必须在这里做——
        「哪些旧事实该保留」是离线语义，不是格式事实，数据层不掺和。空 `facts` 即「清除该 step 的分段」。

        一期不支持同一 (task, step) 跨进程并发跑离线：这段 read-modify-write 没有互斥。
        """
        kept = [
            f for f in inference_store.read_temporal(task_id, step_id)
            if not isinstance(f, TemporalSegment)
        ]
        inference_store.write_temporal(task_id, step_id, kept + facts)

    @staticmethod
    def _maybe_write_debug(spec: OfflineRunSpec, segmenter) -> None:
        """策略若产逐帧调试产物（debug_result 非 None），落一份调试 JSON。

        与 temporal.jsonl 同域，供调试/对比；写失败只告警不影响已成功的事实落盘。
        """
        debug = segmenter.debug_result()
        if debug is None:
            return
        payload = {"task_id": spec.task_id, "step_id": spec.step_id, **debug}
        try:
            inference_store.write_debug_result(spec.task_id, spec.step_id, payload)
        except Exception as e:
            logger.warning(
                "[OfflineRunner] 逐帧调试 JSON 落盘失败 task=%s step=%s: %s",
                spec.task_id, spec.step_id, e,
            )

    @staticmethod
    def _validate(facts: List[TemporalSegment], producer: str) -> List[TemporalSegment]:
        """全量校验 TemporalSegment；任一非法整批失败（不部分写）。

        `producer` 是一等字段，故只校验不盖章——策略自己填错名字要当场报出来，不能替它补。
        """
        for f in facts:
            if not isinstance(f, TemporalSegment):
                raise ValueError(f"segmenter 产出非 TemporalSegment: {type(f).__name__}")
            if f.producer != producer:
                raise ValueError(
                    f"TemporalSegment.producer '{f.producer}' != segmenter name '{producer}'"
                )
            if not (math.isfinite(f.start) and math.isfinite(f.end)):
                raise ValueError(f"TemporalSegment 时间非有限数: start={f.start} end={f.end}")
            if f.start > f.end:
                raise ValueError(f"TemporalSegment start > end: {f.start} > {f.end}")
            if not (0.0 <= f.conf <= 1.0):
                raise ValueError(f"TemporalSegment conf 越界: {f.conf}")
        return facts
