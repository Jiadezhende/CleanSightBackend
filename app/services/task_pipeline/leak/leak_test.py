"""基于 TaskPipeline 的泄漏 + 气泡任务管道服务。

本文件将**原有的两个推理任务**：

- EndoscopeBendingDetectionTask（YOLO 弯折检测）
- BubbleDetectionTask（气泡检测）

包装成两个 ``SubtaskPipelineBase`` 的子任务，在 ``_infer_single_frame`` 阶段
直接复用它们的 ``infer`` 逻辑，从而接入统一的 TaskPipeline 体系。

在任务级聚合阶段，通过 TaskPipelineBase 提供的异步聚合线程，
周期性从各子任务 cache 中取最新结果，完成可视化与状态更新，
并将结果封装为 ``FrameData`` 写入 rt/ca frame cache，
使主进程只需消费 processed frame。
"""

from typing import Any, Dict, Mapping, Optional

import numpy as np
import time
import threading

from app.models.frame import FrameData
from app.services.pipeline_base import JsonDict, SubtaskPipelineBase, TaskPipelineBase
from app.services.infer_task import InferenceTask
from app.settings import settings
from app.services.ai_models.yolo_task import EndoscopeBendingDetectionTask
from app.services.ai_models.bubble_task import BubbleDetectionTask


class BubbleSubtaskPipeline(SubtaskPipelineBase):
    """气泡检测子任务流水线（单帧 + 可选时序）。

    本子任务直接复用 ``BubbleDetectionTask`` 的 ``infer`` 接口，
    将其作为 ``_infer_single_frame`` 阶段的实现。
    """

    def __init__(self, name: str, task: Optional[InferenceTask] = None) -> None:
        # cache 完全由基类内部创建和维护
        super().__init__(name=name)
        self._task = task

    def _infer_single_frame(
        self,
        frame: np.ndarray,
        timestamp: float,
        prev_stage_cache: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        # 若未配置具体任务，则返回空结果，保证流水线可运行
        if self._task is None:
            return {"success": True, "bubble_detected": False, "detections": [], "bubble_count": 0}

        # 从上一阶段缓存中提取 InferenceTask 所需的上下文（如 CleaningTask 等）
        ctx: Dict[str, Any] = {}
        if isinstance(prev_stage_cache, Mapping):
            base_ctx = prev_stage_cache.get("context")
            if isinstance(base_ctx, Mapping):
                ctx = dict(base_ctx)

        try:
            result = self._task.infer(frame, ctx)
            # 适配到子任务统一输出结构，确保包含 bubble_detected 字段
            bubble_detected = bool(result.get("bubble_detected", False))
            success = bool(result.get("success", True))
            out: JsonDict = {**result}
            out["bubble_detected"] = bubble_detected
            out["success"] = success
            return out
        except Exception as e:  # 运行时保护
            return {"success": False, "error": str(e), "bubble_detected": False, "detections": [], "bubble_count": 0}

    def _infer_sequence(self, history):
        # 简单统计最近连续检测到气泡的帧数
        cnt = 0
        for item in reversed(history):
            if item.get("bubble_detected"):
                cnt += 1
            else:
                break
        return {"continuous_bubble_count": cnt}


class BendingSubtaskPipeline(SubtaskPipelineBase):
    """弯折检测子任务流水线（单帧）。

    本子任务直接复用 ``EndoscopeBendingDetectionTask`` 的 ``infer`` 接口，
    作为 ``_infer_single_frame`` 阶段的实现。
    """

    def __init__(self, name: str, task: Optional[InferenceTask] = None) -> None:
        # cache 完全由基类内部创建和维护
        super().__init__(name=name)
        self._task = task

    def _infer_single_frame(
        self,
        frame: np.ndarray,
        timestamp: float,
        prev_stage_cache: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        # 若未配置具体任务，则默认认为无弯折
        if self._task is None:
            return {"success": True, "bending_detected": False, "detections": []}

        # 从缓存中提取 InferenceTask 所需上下文
        ctx: Dict[str, Any] = {}
        if isinstance(prev_stage_cache, Mapping):
            base_ctx = prev_stage_cache.get("context")
            if isinstance(base_ctx, Mapping):
                ctx = dict(base_ctx)

        try:
            result = self._task.infer(frame, ctx)
            bending_detected = bool(result.get("bending_detected", False))
            success = bool(result.get("success", True))
            out: JsonDict = {**result}
            out["bending_detected"] = bending_detected
            out["success"] = success
            return out
        except Exception as e:  # 运行时保护
            return {"success": False, "error": str(e), "bending_detected": False, "detections": []}


class LeakBubblePipelineService(TaskPipelineBase):
    """泄漏 + 气泡 检测任务管道。

    - 并行执行两个子任务：气泡检测、弯曲检测
    - 通过 state 记录连续气泡计数，并在达到阈值时标记步骤完成
    """

    def __init__(
        self,
        executor: Optional[Any] = None,
        bubble_task: Optional[InferenceTask] = None,
        bending_task: Optional[InferenceTask] = None,
        bubble_consecutive_threshold: int = 3,
    ) -> None:
        """创建泄漏 + 气泡检测流水线服务。

        Args:
            executor: 可选线程池，用于子任务并行
            bubble_task: 复用的气泡检测任务实例（若为 None 则内部按配置创建）
            bending_task: 复用的弯折检测任务实例（若为 None 则内部按配置创建）
            bubble_consecutive_threshold: 连续多少帧检测到气泡视为步骤完成
        """

        # 若未显式提供，则根据 settings 创建默认任务实例
        if bubble_task is None:
            try:
                bubble_task = BubbleDetectionTask(
                    model_path=getattr(settings, "bubble_model_path", settings.yolo_model_path),
                    conf_threshold=getattr(settings, "bubble_conf_threshold", 0.25),
                    iou_threshold=getattr(settings, "bubble_iou_threshold", 0.45),
                    enabled=True,
                )
            except Exception as e:  # pragma: no cover - 运行时保护
                print(f"初始化气泡检测任务失败: {e}")
                bubble_task = None

        if bending_task is None:
            try:
                bending_task = EndoscopeBendingDetectionTask(
                    model_path=settings.yolo_model_path,
                    conf_threshold=settings.yolo_conf_threshold,
                    iou_threshold=settings.yolo_iou_threshold,
                    enabled=True,
                )
            except Exception as e:  # pragma: no cover - 运行时保护
                print(f"初始化弯折检测任务失败: {e}")
                bending_task = None

        # 创建子任务实例（cache 由 Subtask 基类内部管理）
        bubble_st = BubbleSubtaskPipeline(name="bubble", task=bubble_task)
        bending_st = BendingSubtaskPipeline(name="bending", task=bending_task)

        super().__init__(
            name="leak_bubble",
            subtasks=[bubble_st, bending_st],
            executor=executor,
            parallel=True,
            enable_async_aggregation=True,
            aggregation_interval=0.03,
        )

        # 保存外部参数到实例，便于 update_state / 可视化 使用
        self._bubble_task = bubble_task
        self._bending_task = bending_task
        self._bubble_threshold = int(bubble_consecutive_threshold)
        # 初始化状态
        self._state = {"step_completed": False, "continuous_bubble_count": 0, "last_timestamp": None}

        # 异步可视化所需的最近一帧原始图像
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_timestamp: float = 0.0

    def infer_frame(
        self,
        frame: np.ndarray,
        timestamp: Optional[float] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """对单帧执行泄漏+气泡任务流水线，并在此处完成可视化。

        与基类不同：
        - 仍返回当帧的聚合 JSON 结果（便于落盘/调试）；
        - 但可视化与最终消息聚合主要通过异步线程在
          ``_visualize_and_update_state`` 中完成，并写入 frame/msg cache。
        """

        ts = float(timestamp or time.time())

        # 先保存最近一帧原始图像，供异步线程使用
        with self._frame_lock:
            self._latest_frame = frame.copy()
            self._latest_timestamp = ts

        # 执行子任务 + 同步构建一次聚合结果（作为返回值使用）
        subtask_results = self._run_subtasks(frame, ts, context)
        message = self.build_message(ts, subtask_results, context)
        self._state = self.update_state(ts, subtask_results, message, context)

        aggregated: JsonDict = {
            "timestamp": ts,
            "task_name": self._name,
            "message": message,
            "subtasks": subtask_results,
            "state": self._state,
        }

        # 注意：此处不再向 rt/ca cache 写入 FrameData，
        # 由异步聚合线程在 _visualize_and_update_state 中统一写入。
        return aggregated

    def _run_subtasks(
        self,
        frame: np.ndarray,
        timestamp: float,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, JsonDict]:
        """覆写基类调度逻辑，将上层 context 透传给两个 InferenceTask。

        与基类不同点：这里不会在 prev_stage_cache 中累积其他子任务结果，
        仅提供 ``{"context": context}``，因为两个 YOLO 任务互不依赖。
        """

        # 统一构造传入子任务的 prev_stage_cache
        prev_stage_cache: Dict[str, Any] = {}
        if context is not None:
            prev_stage_cache["context"] = context

        results: Dict[str, JsonDict] = {}

        if self._parallel and self._executor is not None and len(self._subtasks) > 1:
            from concurrent.futures import Future

            futures: Dict[Future, SubtaskPipelineBase] = {}
            for st in self._subtasks:
                fut = self._executor.submit(st.infer_frame, frame, timestamp, prev_stage_cache)
                futures[fut] = st

            for fut, st in futures.items():
                try:
                    res = fut.result()
                except Exception as e:  # pragma: no cover - 运行时保护
                    res = {"success": False, "error": str(e), "task_name": st.name, "timestamp": timestamp}
                results[st.name] = res
        else:
            for st in self._subtasks:
                try:
                    res = st.infer_frame(frame, timestamp, prev_stage_cache)
                except Exception as e:  # pragma: no cover - 运行时保护
                    res = {"success": False, "error": str(e), "task_name": st.name, "timestamp": timestamp}
                results[st.name] = res

        return results

    def build_message(self, timestamp: float, subtask_results: Mapping[str, JsonDict], context: Optional[Mapping[str, Any]] = None) -> JsonDict:
        alerts = []
        bubble = subtask_results.get("bubble", {})
        bending = subtask_results.get("bending", {})

        if isinstance(bubble, dict) and bubble.get("bubble_detected"):
            alerts.append("bubble_detected")

        # 如果时序统计表明连续气泡达到阈值，添加警报
        seq = bubble.get("sequence") if isinstance(bubble, dict) else None
        if isinstance(seq, dict) and seq.get("continuous_bubble_count", 0) >= self._bubble_threshold:
            alerts.append("continuous_bubble")

        if isinstance(bending, dict) and bending.get("bending_detected"):
            alerts.append("bending_detected")

        return {"timestamp": timestamp, "alerts": alerts}

    def update_state(self, timestamp: float, subtask_results: Mapping[str, JsonDict], message: Mapping[str, Any], context: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        # 读取气泡子任务的连续计数（优先取子任务序列信息）
        bubble = subtask_results.get("bubble", {})
        seq = bubble.get("sequence") if isinstance(bubble, dict) else None
        continuous = 0
        if isinstance(seq, dict):
            continuous = int(seq.get("continuous_bubble_count", 0))
        else:
            # 兜底：如果单帧标记为检测到，则 +1，否则 0
            if bubble.get("bubble_detected"):
                continuous = int(self._state.get("continuous_bubble_count", 0)) + 1
            else:
                continuous = 0

        step_completed = continuous >= self._bubble_threshold

        new_state = {
            "step_completed": bool(step_completed),
            "continuous_bubble_count": continuous,
            "last_timestamp": float(timestamp),
        }

        return new_state

    # ---- 异步聚合钩子实现 ----

    def _visualize_and_update_state(
        self,
        subtask_pos_latest: Mapping[str, Optional[JsonDict]],
        subtask_msg_latest: Mapping[str, Optional[JsonDict]],
    ) -> None:
        """从各子任务最新结果异步聚合，完成可视化与状态更新。

        - 使用子任务最新 msg 结果构造一次聚合 message/state；
        - 在最近一帧原始图像上完成可视化，写入 rt/ca frame cache；
        - 将聚合后的 JSON 结果写入 rt/ca msg cache。
        """

        # 组装类似 subtask_results 的结构，过滤掉 None
        subtask_results: Dict[str, JsonDict] = {}
        for name, res in subtask_msg_latest.items():
            if isinstance(res, dict):
                subtask_results[name] = res

        if not subtask_results:
            return

        # 选取一个代表性的时间戳（这里取子任务结果中的最大 timestamp）
        ts_candidates = [float(res.get("timestamp", 0.0)) for res in subtask_results.values()]
        ts = max(ts_candidates) if ts_candidates else time.time()

        # 基于最新子任务结果重新构建 message/state
        message = self.build_message(ts, subtask_results, context=None)
        self._state = self.update_state(ts, subtask_results, message, context=None)

        aggregated: JsonDict = {
            "timestamp": ts,
            "task_name": self._name,
            "message": message,
            "subtasks": subtask_results,
            "state": self._state,
        }

        # 在最近一帧原始图像上进行可视化
        with self._frame_lock:
            if self._latest_frame is None:
                return
            base = self._latest_frame.copy()

        annotated = base
        bubble_res = subtask_results.get("bubble", {})
        if self._bubble_task is not None and isinstance(bubble_res, dict) and bubble_res.get("success", True):
            try:
                annotated = self._bubble_task.visualize(annotated, bubble_res)
            except Exception as e:  # pragma: no cover - 保护
                print(f"BubbleDetectionTask visualize error (async): {e}")

        bending_res = subtask_results.get("bending", {})
        if self._bending_task is not None and isinstance(bending_res, dict) and bending_res.get("success", True):
            try:
                annotated = self._bending_task.visualize(annotated, bending_res)
            except Exception as e:  # pragma: no cover - 保护
                print(f"EndoscopeBendingDetectionTask visualize error (async): {e}")

        fd = FrameData(timestamp=ts, frame=annotated, inference_result=aggregated)
        self.rt_cache_frame.append(fd)
        self.ca_cache_frame.append(fd)
        self.rt_cache_msg.append(aggregated)
        self.ca_cache_msg.append(aggregated)
