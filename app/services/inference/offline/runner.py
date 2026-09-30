"""离线分割编排层 —— 把 (task_id, step_id) 一次跑通 detections.jsonl → 策略 → temporal.jsonl。

调用方（CLI / 测试）显式给 `(task_id, step_id)`，Runner：
    1. 按 step_id 取 stage 配置，实例化 offline 策略（未配置 / offline 为空 → ValidationError，无兜底）；
    2. `runs.query` 解析一次 run（点名的，或缺省时最新可见的），之后全程只读写它；再一次读完整
       检测序列（点名的 run 不在 → reclaimed；缺省且没有可见 run，或检测序列为空 → skipped）；
    3. 策略 preprocess → segment 产出 TemporalSegment（producer = 策略类名）；
    4. 校验 + 排序，**读回既有事实 → 删掉该 run 全部旧分段、保留 TemporalEvent → 整体写回**。

离线链路只识别稳定存储键 `(task_id, step_id)`；不接 client / CQ / 在线 Operator / 告警 / DB。
落盘全经 `app.storage.inference`（存储根归 `settings`，故本类不收 `base_dir`）。

run 锁定在入口：运行期间同 step 重启，结果仍写回解析出的那个 run；该 run 被 TTL 回收则写入
`FileNotFoundError` → `reclaimed`，不重建目录。「该 run 正在运行」由作业服务在提交时挡（409）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

from app.types.run import RunIdentity
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.config import InferenceConfig, load_stage_config
from app.services.inference.stage_factory import StageFactory
from app.storage import inference as inference_store
from app.storage import runs

logger = logging.getLogger(__name__)

_RECLAIMED_MESSAGE = "run 目录已不在（所在 step 过 TTL 被回收），未写任何结果"


@dataclass(frozen=True)
class OfflineRunSpec:
    """一次离线运行的输入：存储键 + 可选的 run（缺省 = 该 step 最新可见 run）。"""

    task_id: int
    step_id: int
    run_id: Optional[int] = None


@dataclass(frozen=True)
class OfflineRunResult:
    """一次离线运行的结果。status ∈ {completed, skipped, reclaimed}；异常经 run() 抛出，不落此结构。"""

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
        segmenter = StageFactory(config).create_offline_segmenter(stage_key)

        producer = segmenter.name
        run = runs.query(spec.task_id, spec.step_id, spec.run_id)
        if run is None:
            if spec.run_id is not None:
                return OfflineRunResult("reclaimed", producer, 0, _RECLAIMED_MESSAGE)
            return OfflineRunResult("skipped", producer, 0, "该 step 没有可见的 run")
        frames = inference_store.read_detections(run)
        if not frames:
            # 无检测结果：跳过，不覆盖旧事实
            return OfflineRunResult("skipped", producer, 0, "该 step 无检测结果")

        model_input = segmenter.preprocess(frames)
        facts = segmenter.segment(model_input)  # 算法异常向上抛出，不写

        validated = self._validate(facts, producer)
        validated.sort(key=lambda f: (f.start, f.end, f.label))

        # 先旁路、后事实：事实是结果的真源，它落盘即代表本次运行完成；旁路在前，
        # 页面读到新事实时对应的概率必然已是同一次运行的（反序会短暂配上旧概率）。
        try:
            self._maybe_write_label_probs(run, segmenter)
            self._replace_segments(run, validated)
        except FileNotFoundError:
            if runs.query(run.task_id, run.step_id, run.run_id) is not None:
                raise
            return OfflineRunResult("reclaimed", producer, 0, _RECLAIMED_MESSAGE)
        logger.info(
            "[OfflineRunner] completed task=%s step=%s run=%s producer=%s segments=%d",
            run.task_id, run.step_id, run.run_id, producer, len(validated),
        )
        return OfflineRunResult("completed", producer, len(validated))

    @staticmethod
    def _replace_segments(run: RunIdentity, facts: List[TemporalSegment]) -> None:
        """幂等替换该 run 的分段：读回既有 → 丢掉全部旧 TemporalSegment、保留 TemporalEvent → 整体写回。

        一个 stage 至多一个离线模型，换模型重跑时旧模型（旧类名）的分段整体被替换，不与新结果并存。
        `write_temporal` 是**整体替换**，盲写会吃掉在线产出的 `TemporalEvent`，故合并必须在这里做——
        「哪些旧事实该保留」是离线语义，不是格式事实，数据层不掺和。空 `facts` 即「清除该 run 的分段」。

        一期不支持同一 (task, step) 跨进程并发跑离线：这段 read-modify-write 没有互斥。
        """
        kept = [
            f for f in inference_store.read_temporal(run)
            if not isinstance(f, TemporalSegment)
        ]
        inference_store.write_temporal(run, kept + facts)

    @staticmethod
    def _maybe_write_label_probs(run: RunIdentity, segmenter) -> None:
        """策略若产逐帧类别概率（`label_probs()` 非 None），落 `label_probs.npz`。

        旁路不影响主结果：形状不一致或写失败只告警、不落，事实照常写。
        """
        probs = segmenter.label_probs()
        if probs is None:
            return
        problem = _label_probs_problem(probs)
        if problem:
            logger.warning(
                "[OfflineRunner] label_probs 形状不一致，不落盘 task=%s step=%s: %s",
                run.task_id, run.step_id, problem,
            )
            return
        try:
            inference_store.write_label_probs(run, probs)
        except FileNotFoundError:
            raise  # run 已被回收：交给 run() 判成 reclaimed
        except Exception as e:
            logger.warning(
                "[OfflineRunner] label_probs 落盘失败 task=%s step=%s: %s",
                run.task_id, run.step_id, e,
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
