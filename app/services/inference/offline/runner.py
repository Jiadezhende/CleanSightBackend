"""离线分割编排层 —— 把 (task_id, step_id) 一次跑通 detections.jsonl → 策略 → temporal.jsonl。

调用方（CLI / 测试）显式给 `(task_id, step_id[, strategy])`，Runner：
    1. 按 step_id 取 stage 配置，实例化 offline 策略（未配置 / offline 为空 → ValidationError，不兜底 MOCK）；
    2. 一次读该 step 的完整检测序列（为空则 skip）；
    3. 策略 preprocess → segment 产出 TemporalSegment（producer = 策略类名）；
    4. 校验 + 排序，**读回既有事实 → 删掉该 step 全部旧分段、保留 TemporalEvent → 整体写回**。

**换代校验**：读前记下 `detections_stamp`，读完、写前各核对一次；不等即 `superseded`、什么都不写
（输入被追加 = 未封口；被整域删后重建 = 同 step 新一代 run 已开写）。丢弃不重试。
写入不重建目录（`create=False`）：核对之后目录才被回收（TTL）时同样 `superseded`，不留僵尸 step。

离线链路只识别稳定存储键 `(task_id, step_id)`；不接 client / CQ / 在线 Operator / 告警 / DB。
落盘全经 `app.storage.inference`（存储根归 `settings`，故本类不收 `base_dir`）。

调用方仍应只对已停写的 step 提交（运行中的 step 必然 superseded，白算一次）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

from app.domain.temporal import LabelProbs, TemporalSegment
from app.services.inference.config import InferenceConfig, load_stage_config
from app.services.inference.stage_factory import StageFactory
from app.storage import inference as inference_store
from app.utils.exceptions import DirectoryGoneError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfflineRunSpec:
    """一次离线运行的输入：稳定存储键 + 可选策略覆盖。"""

    task_id: int
    step_id: int
    strategy: Optional[str] = None  # 覆盖 stage.offline.class（全限定路径），开发期对比策略用


@dataclass(frozen=True)
class OfflineRunResult:
    """一次离线运行的结果。status ∈ {completed, skipped, superseded}；异常经 run() 抛出，不落此结构。

    superseded = 换代校验未过（输入在运行期间变了），本次什么都没写。
    """

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

        stage_key = config.require_offline(spec.step_id)  # 未配置即 ValidationError，不兜底
        segmenter = StageFactory(config).create_offline_segmenter(stage_key, override_class=spec.strategy)

        producer = segmenter.name
        stamp = inference_store.detections_stamp(spec.task_id, spec.step_id)
        frames = inference_store.read_detections(spec.task_id, spec.step_id)
        if not frames:
            # 无检测结果：跳过，不覆盖旧事实
            return OfflineRunResult("skipped", producer, 0, "该 step 无检测结果")
        if inference_store.detections_stamp(spec.task_id, spec.step_id) != stamp:
            return self._superseded(spec, producer, "读取期间检测结果被改写（未封口或已换代）")

        model_input = segmenter.preprocess(frames)
        facts = segmenter.segment(model_input)  # 算法异常向上抛出，不写

        validated = self._validate(facts, producer)
        validated.sort(key=lambda f: (f.start, f.end, f.label))

        if inference_store.detections_stamp(spec.task_id, spec.step_id) != stamp:
            return self._superseded(spec, producer, "运行期间检测结果被追加或换代，放弃写入")

        # 先旁路、后事实：事实是结果的真源，它落盘即代表本次运行完成；旁路在前，
        # 页面读到新事实时对应的概率必然已是同一次运行的（反序会短暂配上旧概率）。
        try:
            self._maybe_write_label_probs(spec, segmenter)
            self._replace_segments(spec.task_id, spec.step_id, validated)
        except DirectoryGoneError:
            return self._superseded(spec, producer, "检测结果目录已被回收，放弃写入")
        logger.info(
            "[OfflineRunner] completed task=%s step=%s producer=%s segments=%d",
            spec.task_id, spec.step_id, producer, len(validated),
        )
        return OfflineRunResult("completed", producer, len(validated))

    @staticmethod
    def _superseded(spec: OfflineRunSpec, producer: str, message: str) -> OfflineRunResult:
        logger.warning(
            "[OfflineRunner] superseded task=%s step=%s: %s", spec.task_id, spec.step_id, message,
        )
        return OfflineRunResult("superseded", producer, 0, message)

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
        inference_store.write_temporal(task_id, step_id, kept + facts, create=False)

    @staticmethod
    def _maybe_write_label_probs(spec: OfflineRunSpec, segmenter) -> None:
        """策略若产逐帧类别概率（`label_probs()` 非 None），落 `label_probs.npz`。

        旁路不影响主结果：形状不一致或写失败只告警、不落，事实照常写。例外是目录已被回收
        （`DirectoryGoneError`）：事实也写不成，上抛给 run() 判 superseded。
        """
        probs = segmenter.label_probs()
        if probs is None:
            return
        problem = _label_probs_problem(probs)
        if problem:
            logger.warning(
                "[OfflineRunner] label_probs 形状不一致，不落盘 task=%s step=%s: %s",
                spec.task_id, spec.step_id, problem,
            )
            return
        try:
            inference_store.write_label_probs(spec.task_id, spec.step_id, probs, create=False)
        except DirectoryGoneError:
            raise
        except Exception as e:
            logger.warning(
                "[OfflineRunner] label_probs 落盘失败 task=%s step=%s: %s",
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


def _label_probs_problem(probs: LabelProbs) -> str:
    """LabelProbs 形状一致性检查，合法返回空串。"""
    ts, p = np.asarray(probs.ts), np.asarray(probs.probs)
    if ts.ndim != 1 or p.ndim != 2:
        return f"期望 ts [T] 与 probs [T,C]，得到 ts{ts.shape} probs{p.shape}"
    if ts.shape[0] != p.shape[0]:
        return f"ts 与 probs 行数不等：{ts.shape[0]} != {p.shape[0]}"
    if len(probs.labels) != p.shape[1]:
        return f"labels 数与 probs 列数不等：{len(probs.labels)} != {p.shape[1]}"
    return ""
