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

from typing import Any, Dict, Mapping, Optional, List, Sequence

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

    def infer_batch(
        self,
        frames: Sequence[np.ndarray],
        timestamps: Optional[Sequence[float]] = None,
        prev_stage_cache: Optional[Mapping[str, Any]] = None,
    ) -> List[JsonDict]:
        """批量气泡检测：优先使用底层 InferenceTask 的 infer_batch 接口。

        为保持与单帧接口一致，本方法内部仍通过 ``_process_single_result``
        更新 history 与四类 cache。
        """

        if self._task is None:
            # 无底层任务时退回到基类默认逐帧实现
            return super().infer_batch(frames, timestamps, prev_stage_cache)

        n = len(frames)
        if n == 0:
            return []

        # 生成/对齐时间戳
        if timestamps is not None and len(timestamps) == n:
            ts_list = [float(t) for t in timestamps]
        else:
            base = time.time()
            ts_list = [float(base + i * 1e-3) for i in range(n)]

        # 构造 batch 上下文列表
        base_ctx: Dict[str, Any] = {}
        if isinstance(prev_stage_cache, Mapping):
            ctx0 = prev_stage_cache.get("context")
            if isinstance(ctx0, Mapping):
                base_ctx = dict(ctx0)

        contexts: List[Dict[str, Any]] = [dict(base_ctx) for _ in range(n)]

        # 调用底层批量推理
        try:
            batch_results = self._task.infer_batch(list(frames), contexts)
        except Exception as e:  # pragma: no cover - 运行时保护
            print(f"BubbleSubtaskPipeline.infer_batch error, fallback to per-frame: {e}")
            return super().infer_batch(frames, timestamps, prev_stage_cache)

        out: List[JsonDict] = []
        for res, ts in zip(batch_results, ts_list):
            try:
                bubble_detected = bool(res.get("bubble_detected", False))
                success = bool(res.get("success", True))
                single_res: JsonDict = {**res}
                single_res["bubble_detected"] = bubble_detected
                single_res["success"] = success
            except Exception:  # pragma: no cover - 防御
                single_res = {"success": False, "error": "invalid_batch_result", "bubble_detected": False, "detections": [], "bubble_count": 0}

            merged = self._process_single_result(single_res, ts)
            out.append(merged)

        return out


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

    def infer_batch(
        self,
        frames: Sequence[np.ndarray],
        timestamps: Optional[Sequence[float]] = None,
        prev_stage_cache: Optional[Mapping[str, Any]] = None,
    ) -> List[JsonDict]:
        """批量弯折检测：优先使用底层 InferenceTask 的 infer_batch 接口。"""

        if self._task is None:
            return super().infer_batch(frames, timestamps, prev_stage_cache)

        n = len(frames)
        if n == 0:
            return []

        # 生成/对齐时间戳
        if timestamps is not None and len(timestamps) == n:
            ts_list = [float(t) for t in timestamps]
        else:
            base = time.time()
            ts_list = [float(base + i * 1e-3) for i in range(n)]

        # 构造 batch 上下文列表
        base_ctx: Dict[str, Any] = {}
        if isinstance(prev_stage_cache, Mapping):
            ctx0 = prev_stage_cache.get("context")
            if isinstance(ctx0, Mapping):
                base_ctx = dict(ctx0)

        contexts: List[Dict[str, Any]] = [dict(base_ctx) for _ in range(n)]

        try:
            batch_results = self._task.infer_batch(list(frames), contexts)
        except Exception as e:  # pragma: no cover - 运行时保护
            print(f"BendingSubtaskPipeline.infer_batch error, fallback to per-frame: {e}")
            return super().infer_batch(frames, timestamps, prev_stage_cache)

        out: List[JsonDict] = []
        for res, ts in zip(batch_results, ts_list):
            try:
                bending_detected = bool(res.get("bending_detected", False))
                success = bool(res.get("success", True))
                single_res: JsonDict = {**res}
                single_res["bending_detected"] = bending_detected
                single_res["success"] = success
            except Exception:  # pragma: no cover
                single_res = {"success": False, "error": "invalid_batch_result", "bending_detected": False, "detections": []}

            merged = self._process_single_result(single_res, ts)
            out.append(merged)

        return out


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

        # 异步可视化所需的上一次推理结果（用于沿用）
        self._result_lock = threading.Lock()
        self._last_inference_result: Optional[JsonDict] = None
        
        # 调试计数器（监控帧产出）
        self._debug_frame_count = 0
        self._debug_last_log_time = time.time()
        
        print(f"[LeakBubblePipeline] 初始化完成，异步聚合已启动 (interval={self._aggregation_interval:.3f}s, ~{1/self._aggregation_interval:.1f}fps)")

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

        # 保存最新推理结果供异步聚合沿用（当没有新推理时）
        with self._result_lock:
            self._last_inference_result = aggregated

        # 注意：此处不再向 rt/ca cache 写入 FrameData，
        # 由异步聚合线程在 _visualize_and_update_state 中统一写入。
        return aggregated

    def infer_batch(
        self,
        frames: Sequence[np.ndarray],
        timestamps: Optional[Sequence[float]] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> List[JsonDict]:
        """TaskPipeline 级批量推理接口。

        - 调用各子任务的 ``infer_batch``（内部会使用 YOLO 的 infer_batch）；
        - 为每帧构建一次聚合 message/state，主要用于调试/日志；
        - 实际可视化与聚合写 cache 仍由异步线程完成。
        """

        n = len(frames)
        if n == 0:
            return []

        # 生成/对齐时间戳
        if timestamps is not None and len(timestamps) == n:
            ts_list = [float(t) for t in timestamps]
        else:
            base = time.time()
            ts_list = [float(base + i * 1e-3) for i in range(n)]

        # 统一构造传入子任务的 prev_stage_cache
        prev_stage_cache: Dict[str, Any] = {}
        if context is not None:
            prev_stage_cache["context"] = context

        # 找到两个子任务实例
        bubble_st: Optional[SubtaskPipelineBase] = None
        bending_st: Optional[SubtaskPipelineBase] = None
        for st in self.subtasks:
            if st.name == "bubble":
                bubble_st = st
            elif st.name == "bending":
                bending_st = st

        bubble_results: List[JsonDict] = []
        bending_results: List[JsonDict] = []

        if bubble_st is not None:
            bubble_results = bubble_st.infer_batch(frames, ts_list, prev_stage_cache)
        else:
            bubble_results = [{"success": False, "error": "bubble_subtask_missing"} for _ in range(n)]

        if bending_st is not None:
            bending_results = bending_st.infer_batch(frames, ts_list, prev_stage_cache)
        else:
            bending_results = [{"success": False, "error": "bending_subtask_missing"} for _ in range(n)]

        out: List[JsonDict] = []
        for ts, br, er in zip(ts_list, bubble_results, bending_results):
            subtask_results = {"bubble": br, "bending": er}
            message = self.build_message(ts, subtask_results, context)
            self._state = self.update_state(ts, subtask_results, message, context)
            aggregated: JsonDict = {
                "timestamp": ts,
                "task_name": self._name,
                "message": message,
                "subtasks": subtask_results,
                "state": self._state,
            }
            out.append(aggregated)

        # 保存最后一个推理结果供异步聚合沿用
        if out:
            with self._result_lock:
                self._last_inference_result = out[-1]

        return out

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

        优化策略：
        - 从 ClientQueues 获取最新原始帧（解耦推理速率和可视化速率）
        - 如果有新的子任务推理结果，则使用新结果；否则沿用上一个推理结果
        - 保证 processed_frame 的稳定产出速率
        """

        # 1. 获取最新原始帧（从 ClientQueues）
        client_queues = self.get_client_queues()
        if client_queues is None:
            return
        
        raw_frame_data = client_queues.get_latest_raw_frame()
        if raw_frame_data is None:
            return
        
        base_frame, frame_timestamp = raw_frame_data

        # 2. 确定使用的推理结果：优先使用新结果，否则沿用上一个
        subtask_results: Dict[str, JsonDict] = {}
        for name, res in subtask_msg_latest.items():
            if isinstance(res, dict):
                subtask_results[name] = res

        # 如果没有新的推理结果，沿用上一个推理结果（或使用空结果）
        has_new_inference = bool(subtask_results)
        
        if not subtask_results:
            with self._result_lock:
                if self._last_inference_result is None:
                    # 还没有任何推理结果，使用空结果（输出原始帧，不带标注）
                    aggregated = {
                        "timestamp": frame_timestamp,
                        "task_name": self._name,
                        "message": {"timestamp": frame_timestamp, "alerts": []},
                        "subtasks": {},
                        "state": self._state,
                    }
                    subtask_results = {}  # 空结果，跳过可视化
                else:
                    # 沿用上一个推理结果，但更新时间戳为当前帧时间戳
                    last_result = self._last_inference_result.copy()
                    last_result["timestamp"] = frame_timestamp
                    # 关键：获取上一次的 subtasks 用于可视化
                    subtask_results = last_result.get("subtasks", {})
                    if isinstance(subtask_results, dict):
                        subtask_results = dict(subtask_results)  # 复制一份避免修改原数据
                    else:
                        subtask_results = {}
                    aggregated = last_result
        else:
            # 有新的推理结果，构建新的聚合结果
            ts = frame_timestamp  # 使用当前帧的时间戳
            message = self.build_message(ts, subtask_results, context=None)
            self._state = self.update_state(ts, subtask_results, message, context=None)
            aggregated = {
                "timestamp": ts,
                "task_name": self._name,
                "message": message,
                "subtasks": subtask_results,
                "state": self._state,
            }

        # 3. 在最新原始帧上进行可视化
        annotated = base_frame
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

        # 4. 写入 frame 和 msg cache
        fd = FrameData(timestamp=frame_timestamp, frame=annotated, inference_result=aggregated)
        self.rt_cache_frame.append(fd)
        self.ca_cache_frame.append(fd)
        self.rt_cache_msg.append(aggregated)
        self.ca_cache_msg.append(aggregated)
        
        # 调试日志：每秒输出一次统计信息
        self._debug_frame_count += 1
        current_time = time.time()
        if current_time - self._debug_last_log_time >= 5.0:  # 每5秒输出一次
            elapsed = current_time - self._debug_last_log_time
            fps = self._debug_frame_count / elapsed
            print(f"[LeakBubblePipeline] 异步聚合产出: {self._debug_frame_count}帧/{elapsed:.1f}秒 = {fps:.1f}fps | "
                  f"新推理: {has_new_inference} | rt_cache: {len(self.rt_cache_frame)} | ca_cache: {len(self.ca_cache_frame)}")
            self._debug_frame_count = 0
            self._debug_last_log_time = current_time
