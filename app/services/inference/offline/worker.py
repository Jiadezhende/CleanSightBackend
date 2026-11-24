"""离线分析编排。run 提交分割产物后核对同一 run，check 单独核对。

一次执行只处理入口解析出的 RunIdentity。分割写回遇到 run 回收时返回 reclaimed。
规则判定失败只追加到 run 的结果说明，独立 check 则返回 error。
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import numpy as np

from app.types.run import RunIdentity
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.config import InferenceConfig, StageConfig, load_stage_config
from app.services.inference.stage_factory import StageFactory
from app.storage import hls
from app.storage import inference as inference_store
from app.storage import runs

from .compliance import checker
from .compliance.rules import Verdict
from .segmentation.segmenter import OfflineContext

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
    run_id: Optional[int] = None  # 实际锁定的 run；completed 时必有
    alarm_count: int | None = None


@dataclass(frozen=True)
class OfflineCheckResult:
    status: str
    run_id: int | None
    alarm_count: int | None = None
    message: str = ""
    verdicts: Sequence[Verdict] = ()


class OfflineWorker:
    """同步执行一次离线分析或规则判定。"""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        config: Optional[InferenceConfig] = None,
    ):
        self._config_path = config_path
        self._config = config  # 显式注入优先（测试用）；否则走 load_stage_config 单例

    def _get_inference_config(self) -> InferenceConfig:
        return self._config if self._config is not None else load_stage_config(self._config_path)

    def run(self, spec: OfflineRunSpec) -> OfflineRunResult:
        config = self._get_inference_config()
        stage_key = config.require_offline(spec.step_id)  # 未配置即 ValidationError，不兜底
        stage = config.stages[stage_key]
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

        segments, label_probs = segmenter.infer(frames, self._build_run_context(run))
        segments = sorted(segments, key=lambda segment: (segment.start, segment.end, segment.label))

        # 概率旁路先写，动作段提交后规则判定才开始。
        try:
            self._try_write_label_probs(run, label_probs)
            self._replace_segments(run, segments)
        except FileNotFoundError:
            if runs.query(run.task_id, run.step_id, run.run_id) is not None:
                raise
            return OfflineRunResult("reclaimed", producer, 0, _RECLAIMED_MESSAGE)
        checked = self._check_compliance(run, stage)
        message = f"规则核对失败: {checked.message}" if checked.status == "error" else checked.message
        logger.info(
            "[OfflineWorker] completed task=%s step=%s run=%s producer=%s segments=%d",
            run.task_id, run.step_id, run.run_id, producer, len(segments),
        )
        return OfflineRunResult("completed", producer, len(segments), message, run.run_id, checked.alarm_count)

    def check(self, spec: OfflineRunSpec, *, dry_run: bool = False) -> OfflineCheckResult:
        run = runs.query(spec.task_id, spec.step_id, spec.run_id)
        if run is None:
            return OfflineCheckResult("error", spec.run_id, message="找不到可见的 run")
        try:
            stage = self._get_inference_config().get_stage_config(str(spec.step_id))
        except Exception as exc:
            logger.exception(
                "规则核对失败 task=%s step=%s run=%s: %s", run.task_id, run.step_id, run.run_id, exc,
            )
            return OfflineCheckResult("error", run.run_id, message=str(exc))
        if stage is None:
            return OfflineCheckResult("error", run.run_id, message=f"step {spec.step_id} 未在推理配置中定义")
        return self._check_compliance(run, stage, dry_run=dry_run)

    @staticmethod
    def _check_compliance(run: RunIdentity, stage: StageConfig, *, dry_run: bool = False) -> OfflineCheckResult:
        try:
            result = checker.check_run(run, stage.compliance, stage=stage.alias, dry_run=dry_run)
        except Exception as exc:
            logger.exception(
                "规则核对失败 task=%s step=%s run=%s: %s", run.task_id, run.step_id, run.run_id, exc,
            )
            return OfflineCheckResult("error", run.run_id, message=str(exc))
        return OfflineCheckResult(result.status, run.run_id, result.alarm_count, result.message, result.verdicts)

    @staticmethod
    def _build_run_context(run: RunIdentity) -> OfflineContext:
        """绑定到本次锁定 run 的资源；取帧只走 raw 轨（帧 ts 与 detections 位级相等）。"""
        return OfflineContext(
            video=functools.partial(hls.iter_frames, run),
            load_cache=functools.partial(_read_run_cache, run),
            save_cache=functools.partial(_try_write_run_cache, run),
        )

    @staticmethod
    def _replace_segments(run: RunIdentity, segments: List[TemporalSegment]) -> None:
        """幂等替换该 run 的分段：读回既有 → 丢掉全部旧 TemporalSegment、保留 TemporalEvent → 整体写回。

        一个 stage 至多一个离线模型，换模型重跑时旧模型（旧类名）的分段整体被替换，不与新结果并存。
        `write_temporal` 是**整体替换**，盲写会吃掉在线产出的 `TemporalEvent`，故合并必须在这里做——
        「哪些旧事实该保留」是离线语义，不是格式事实，数据层不掺和。空 `segments` 即「清除该 run 的分段」。

        一期不支持同一 (task, step) 跨进程并发跑离线：这段 read-modify-write 没有互斥。
        """
        kept = [
            f for f in inference_store.read_temporal(run)
            if not isinstance(f, TemporalSegment)
        ]
        inference_store.write_temporal(run, kept + segments)

    @staticmethod
    def _try_write_label_probs(run: RunIdentity, probs: LabelProbs | None) -> None:
        """策略若产逐帧类别概率，落 `label_probs.npz`。

        普通写入失败只告警，动作段照常提交。run 回收由 run() 处理。
        """
        if probs is None:
            return
        try:
            inference_store.write_label_probs(run, probs)
        except FileNotFoundError:
            raise  # run 已被回收：交给 run() 判成 reclaimed
        except Exception as e:
            logger.warning(
                "[OfflineWorker] label_probs 落盘失败 task=%s step=%s: %s",
                run.task_id, run.step_id, e,
            )


def _read_run_cache(run: RunIdentity, name: str) -> Optional[Dict[str, np.ndarray]]:
    """缓存读坏了当作没有（策略重算并覆盖），不让一份坏缓存卡死这个 run 的离线作业。"""
    try:
        return inference_store.read_offline_cache(run, name)
    except (OSError, ValueError, KeyError) as e:
        logger.warning("[OfflineWorker] 缓存不可读，按未命中处理 task=%s step=%s name=%s: %s",
                       run.task_id, run.step_id, name, e)
        return None


def _try_write_run_cache(run: RunIdentity, name: str, arrays: Mapping[str, np.ndarray]) -> None:
    """缓存写失败只告警：本次结果不依赖它；run 已被回收时后面写事实会判 reclaimed。"""
    try:
        inference_store.write_offline_cache(run, name, arrays)
    except OSError as e:
        logger.warning("[OfflineWorker] 缓存写失败 task=%s step=%s name=%s: %s",
                       run.task_id, run.step_id, name, e)
