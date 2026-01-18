from __future__ import annotations

from app.models.frame import FrameData

"""通用推理流水线基类。

当前先不依赖现有 InferenceTask 结构，仅提供 TaskPipelineBase 和
SubtaskPipelineBase 的抽象设计，用于后续重构推理流水线。

设计要点：
- SubtaskPipelineBase：
    - 对外统一接口 infer_frame(frame, timestamp, prev_stage_cache)
    - 必须实现单帧处理阶段 _infer_single_frame
    - 可选实现时间序列处理 _infer_sequence
    - 内部自维护 4 组 cache（不从外部注入）：
        两阶段之间一个实时 rt_cache_pos，存 JSON-able dict（位置信息）
        两阶段之间一个持久化 ca_cache_pos，存 JSON-able dict（位置信息）
        末尾一个实时 rt_cache_msg，存 JSON-able dict（语义信息）
        末尾一个持久化 ca_cache_msg，存 JSON-able dict（位置+语义信息，用于数据落盘）
    - 内部维护历史结果队列，供时间序列处理使用
    - 每次调用 infer_frame 会写入该 subtask 的 cache

- TaskPipelineBase：
    - 对外统一接口 infer_frame(frame, timestamp, context)
    - 内部调度多个 SubtaskPipelineBase，实现并行组合
    - 为所有 subtasks 分配并管理 cache（不从外部注入）
    - 异步将各 rt_cache_pos 的结果聚合并进行可视化渲染，写入内部维护的 rt_cache_frame 和 ca_cache_frame
    - 异步消费各 rt_cache_msg 的结果并更新 TaskPipelineBase 内部的任务状态字典 state
    - 异步将各 ca_cache_msg 的结果聚合，写入内部维护的 ca_cache_msg
    - 主进程只能“异步读取/消费”这些聚合 cache，本身不写入：
        - rt_cache_msg：用于实时渲染/前端展示
        - ca_cache_msg：用于信息落盘/回放
        - ca_cache_frame：用于处理后的视频落盘/回放

主进程只需要访问 TaskPipelineBase 提供的这些聚合 cache 进行落盘、转发等操作；
cache 的写入严格由各 Pipeline 内部控制，实现“内部写、外部读/消费”。
"""

import time
from abc import ABC, abstractmethod
from collections import deque
from concurrent.futures import Executor, Future
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import threading

import numpy as np

JsonDict = Dict[str, Any]


class SubtaskPipelineBase(ABC):
    """子任务推理流水线基类。

    一个 SubtaskPipeline 通常负责：
    - 单帧处理（必选）：从原始帧中提取当前帧的检测/分析结果
    - 时间序列处理（可选）：基于历史结果进行平滑、统计等
    - 通过四类 cache 向外暴露中间结果：
        * rt_cache_pos: 实时位置/检测结果（JSON-able dict）
        * ca_cache_pos: 持久化位置/检测结果（JSON-able dict）
        * rt_cache_msg: 实时语义结果（JSON-able dict）
        * ca_cache_msg: 持久化位置+语义结果（JSON-able dict）

    为兼容历史代码，仍保留 ``cache`` 属性，等价于 ``rt_cache_pos``（只读访问）。
    子类只需关注本子任务的业务逻辑；缓存、基础字段补全等通用逻辑由基类实现。
    """

    def __init__(
        self,
        name: str,
        max_cache_size: int = 256,
    ) -> None:
        """创建子任务流水线基类。

        Args:
            name: 子任务名称
            max_cache_size: 内部历史长度上限
        """

        self._name = name
        self._max_cache_size = max_cache_size

        # cache 完全内部管理：统一按 max_cache_size 创建四类队列
        self._rt_cache_pos: Deque[JsonDict] = deque(maxlen=max_cache_size)
        self._ca_cache_pos: Deque[JsonDict] = deque(maxlen=max_cache_size)
        self._rt_cache_msg: Deque[JsonDict] = deque(maxlen=max_cache_size)
        self._ca_cache_msg: Deque[JsonDict] = deque(maxlen=max_cache_size)

        # 可选：内部历史缓存，用于时间序列处理
        self._history: Deque[JsonDict] = deque(maxlen=max_cache_size)

    # ---- 属性 ----
    @property
    def name(self) -> str:
        return self._name

    @property
    def cache(self) -> Deque[JsonDict]:
        """兼容旧接口：返回位置结果的实时 cache。"""
        return self._rt_cache_pos

    @property
    def rt_cache_pos(self) -> Deque[JsonDict]:
        """实时位置/检测结果 cache（只读视图）。"""
        return self._rt_cache_pos

    @property
    def ca_cache_pos(self) -> Deque[JsonDict]:
        """持久化位置/检测结果 cache（只读视图）。"""
        return self._ca_cache_pos

    @property
    def rt_cache_msg(self) -> Deque[JsonDict]:
        """实时语义结果 cache（只读视图）。"""
        return self._rt_cache_msg

    @property
    def ca_cache_msg(self) -> Deque[JsonDict]:
        """持久化位置+语义结果 cache（只读视图）。"""
        return self._ca_cache_msg

    # ---- 对外主入口 ----
    def infer_frame(
        self,
        frame: np.ndarray,
        timestamp: Optional[float] = None,
        prev_stage_cache: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """对单帧进行推理（单帧 + 可选时间序列），并写入四类 cache。

        Args:
            frame: 当前帧图像（BGR/RGB 由子类约定）
            timestamp: 帧时间戳（秒）；None 则使用 time.time()
            prev_stage_cache: 上一阶段/上层流水线的聚合结果快照，可选

        Returns:
            该子任务在当前帧上的 JSON 结果（已包含基础字段和合并后的时序信息）。
        """
        ts = float(timestamp or time.time())

        # 1. 单帧处理（必须由子类实现）
        single_res = self._infer_single_frame(frame, ts, prev_stage_cache)

        # 2. 统一走内部单帧结果处理逻辑，保证与批量接口一致
        return self._process_single_result(single_res, ts)

    def infer_batch(
        self,
        frames: Sequence[np.ndarray],
        timestamps: Optional[Sequence[float]] = None,
        prev_stage_cache: Optional[Mapping[str, Any]] = None,
    ) -> List[JsonDict]:
        """默认批量推理实现：逐帧调用 ``infer_frame``。

        子类可以覆写本方法以利用底层模型的批量接口（如 YOLO.detect_batch），
        但应在内部调用 ``_process_single_result`` 以保证 cache/history 行为一致。
        """

        n = len(frames)
        if n == 0:
            return []

        # 为每帧生成/对齐时间戳
        if timestamps is not None and len(timestamps) == n:
            ts_list = [float(t) for t in timestamps]
        else:
            base = time.time()
            ts_list = [float(base + i * 1e-3) for i in range(n)]

        out: List[JsonDict] = []
        for frame, ts in zip(frames, ts_list):
            single_res = self._infer_single_frame(frame, ts, prev_stage_cache)
            merged = self._process_single_result(single_res, ts)
            out.append(merged)

        return out

    # ---- 子类需要/可以实现的接口 ----

    @abstractmethod
    def _infer_single_frame(
        self,
        frame: np.ndarray,
        timestamp: float,
        prev_stage_cache: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """单帧推理阶段（必须实现）。

        要求：
        - 返回 JSON 可序列化的 dict
        - 建议至少包含：位置信息（如 bboxes/keypoints）、score、内部状态等
        - 不必填充 timestamp/task_name，基类会统一补全
        """

    def _infer_sequence(self, history: Sequence[JsonDict]) -> Optional[JsonDict]:
        """时间序列处理阶段（可选）。

        默认返回 None，即不进行时序处理。
        子类如需进行 smoothing/统计，可覆写本方法。
        """

        return None

    def _merge_results(
        self,
        single_res: JsonDict,
        seq_res: Optional[JsonDict],
    ) -> JsonDict:
        """合并单帧结果与时间序列结果的默认策略。

        默认行为：若存在时序结果，则将其挂在 "sequence" 字段下。
        子类如需自定义合并方式，可覆写本方法。
        """

        if not seq_res:
            return single_res
        merged: JsonDict = {**single_res}
        merged["sequence"] = seq_res
        return merged

    # ---- 工具方法 ----

    def _push_to_cache(self, cache: Deque[JsonDict], item: JsonDict) -> None:
        """向指定 cache 追加一条记录，并控制长度上限。"""

        cache.append(item)
        while len(cache) > self._max_cache_size:
            cache.popleft()

    def _ensure_basic_fields(self, res: JsonDict, ts: float) -> JsonDict:
        """补全通用字段：timestamp / task_name。"""

        res.setdefault("timestamp", ts)
        res.setdefault("task_name", self._name)
        return res

    def _process_single_result(self, single_res: JsonDict, ts: float) -> JsonDict:
        """统一处理单帧推理结果：补全字段、更新 history 与四类 cache。

        该方法被 ``infer_frame`` 与 ``infer_batch`` 内部复用，
        保证无论单帧还是批量接口，cache/history 行为完全一致。
        """

        # 补全基础字段
        single_res = self._ensure_basic_fields(single_res, ts)

        # 历史记录
        self._history.append(single_res)

        # 位置类 cache（实时/持久化）
        pos_result = single_res
        self._push_to_cache(self._rt_cache_pos, pos_result)
        self._push_to_cache(self._ca_cache_pos, pos_result)

        # 时间序列处理
        seq_res = self._infer_sequence(self._history)
        if seq_res is not None:
            seq_res = self._ensure_basic_fields(seq_res, ts)

        # 合并结果并写入语义类 cache
        merged = self._merge_results(single_res, seq_res)
        self._push_to_cache(self._rt_cache_msg, merged)
        self._push_to_cache(self._ca_cache_msg, merged)

        return merged


class TaskPipelineBase(ABC):
    """任务级推理流水线基类，**一个清洗步骤的最小执行单元**。

    本质上是对一组 SubtaskPipeline 的包装，负责：

    - 对外主入口 ``infer_frame(frame, timestamp, context)``：
        * 轮询 / 调度所有子任务，对单帧执行推理
        * 汇总得到统一的聚合结果 JSON（message + subtasks）
        * 维护步骤级别的状态字典（state），用于步骤完成判定等逻辑
    - 内部维护：
        * ``subtasks``: 子任务流水线列表
        * ``state``: 步骤状态字典（例如 step_completed / progress / counters 等）
    """

    def __init__(
        self,
        name: str,
        subtasks: Sequence[SubtaskPipelineBase],
        executor: Optional[Executor] = None,
        parallel: bool = True,
        max_cache_size: int = 256,
        enable_async_aggregation: bool = False,
        aggregation_interval: float = 1/30, # 聚合频率，决定了推理返流的帧率？
    ) -> None:
        self._name = name
        self._subtasks: List[SubtaskPipelineBase] = list(subtasks)
        # cache 完全内部管理：统一按 max_cache_size 创建队列
        self._rt_cache_frame: Deque[FrameData] = deque(maxlen=max_cache_size)
        self._ca_cache_frame: Deque[FrameData] = deque(maxlen=max_cache_size)
        self._rt_cache_msg: Deque[JsonDict] = deque(maxlen=max_cache_size)
        self._ca_cache_msg: Deque[JsonDict] = deque(maxlen=max_cache_size)
        self._executor = executor
        self._parallel = parallel and executor is not None
        # 步骤级状态字典：用于状态检测、步骤是否完成等逻辑
        # 约定：子类在 update_state 中维护该状态，
        # 至少应包含一个布尔字段 step_completed 用于标记步骤是否完成。
        self._state: Dict[str, Any] = {}

        # 异步聚合相关：可选的后台线程模板
        self._enable_async_aggregation = bool(enable_async_aggregation)
        self._aggregation_interval = float(aggregation_interval)
        self._stop_event = threading.Event()
        self._aggregation_thread: Optional[threading.Thread] = None
        
        # ClientQueues 引用（用于异步聚合时获取最新原始帧）
        self._client_queues: Optional[Any] = None

        if self._enable_async_aggregation:
            self._start_aggregation_thread()

    # ---- 属性 ----
    @property
    def name(self) -> str:
        return self._name

    @property
    def subtasks(self) -> Sequence[SubtaskPipelineBase]:
        return self._subtasks

    @property
    def rt_cache_frame(self) -> Deque[FrameData]:
        """实时帧数据 cache（只读视图）。"""
        return self._rt_cache_frame

    @property
    def ca_cache_frame(self) -> Deque[FrameData]:
        """持久化帧数据 cache（只读视图）。"""
        return self._ca_cache_frame
    
    @property
    def rt_cache_msg(self) -> Deque[JsonDict]:
        """实时聚合结果 cache（只读视图）。"""
        return self._rt_cache_msg
    
    @property
    def ca_cache_msg(self) -> Deque[JsonDict]:
        """持久化聚合结果 cache（只读视图）。"""
        return self._ca_cache_msg

    @property
    def state(self) -> Mapping[str, Any]:
        """当前步骤的状态字典（只读视图）。

        典型字段示例：
        - step_completed: bool  步骤是否已满足完成条件
        - progress: float       0~1 进度估计
        - last_timestamp: float 最近一次更新的时间戳
        """
        return self._state
    
    def set_client_queues(self, client_queues: Any) -> None:
        """设置 ClientQueues 实例，用于异步聚合时获取最新原始帧。"""
        self._client_queues = client_queues
    
    def get_client_queues(self) -> Optional[Any]:
        """获取 ClientQueues 实例。"""
        return self._client_queues

    def is_step_completed(self) -> bool:
        """快捷判断当前步骤是否已完成。

        约定：state 中的 ``step_completed`` 字段为 True 即表示完成。
        若不存在该字段，则视为未完成。
        """

        return bool(self._state.get("step_completed", False))

    # ---- 异步聚合线程控制 ----

    def _start_aggregation_thread(self) -> None:
        """启动默认的后台聚合线程（模板）。

        默认行为：周期性从各 Subtask 的 rt_cache_pos/rt_cache_msg 抽取最新结果，
        并调用 ``_visualize_and_update_state`` 钩子。
        子类可以覆写该钩子，在其中完成：
        - 可视化渲染并写入 rt_cache_frame/ca_cache_frame；
        - 根据子任务结果更新 self._state；
        - （可选）写入/更新聚合后的 rt_cache/ca_cache。
        """

        if self._aggregation_thread is not None and self._aggregation_thread.is_alive():
            return

        thread = threading.Thread(target=self._aggregation_loop, daemon=True)
        self._aggregation_thread = thread
        thread.start()
        print(f"[TaskPipeline] 异步聚合线程已启动: {self._name} (interval={self._aggregation_interval:.3f}s)")

    def stop(self) -> None:
        """停止后台聚合线程（如已启用）。"""

        if not self._enable_async_aggregation:
            return
        self._stop_event.set()
        if self._aggregation_thread is not None and self._aggregation_thread.is_alive():
            self._aggregation_thread.join(timeout=1.0)

    def _aggregation_loop(self) -> None:
        """默认聚合循环：定期从子任务 cache 抽取快照并调用钩子。"""

        while not self._stop_event.is_set():
            try:
                self._aggregate_once_from_subtasks()
            except Exception as e:
                # 运行时保护：避免线程因异常退出，但打印错误便于调试
                print(f"[TaskPipeline] 异步聚合异常 ({self._name}): {e}")
                import traceback
                traceback.print_exc()
            # 使用 Event.wait 便于及时响应 stop 信号
            self._stop_event.wait(self._aggregation_interval)

    def _aggregate_once_from_subtasks(self) -> None:
        """从各子任务 cache 抽取一次最新结果，并调用异步聚合钩子。"""

        subtask_pos_latest: Dict[str, Optional[JsonDict]] = {}
        subtask_msg_latest: Dict[str, Optional[JsonDict]] = {}

        for st in self._subtasks:
            try:
                pos_cache = st.rt_cache_pos
                msg_cache = st.rt_cache_msg
            except Exception:
                continue

            subtask_pos_latest[st.name] = pos_cache[-1] if pos_cache else None
            subtask_msg_latest[st.name] = msg_cache[-1] if msg_cache else None

        if subtask_pos_latest or subtask_msg_latest:
            self._visualize_and_update_state(subtask_pos_latest, subtask_msg_latest)

    # ---- 对外主入口 ----
    def infer_frame(
        self,
        frame: np.ndarray,
        timestamp: Optional[float] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """对单帧执行整个任务流水线，并将聚合结果写入两个 cache。

        Args:
            frame: 当前帧
            timestamp: 时间戳（秒）
            context: 外部上下文（如 CleaningTask / client_id / 业务参数等）

        Returns:
            聚合后的 JSON 结果，通常结构类似：
            {
                "timestamp": ts,
                "task_name": self.name,
                "message": {...},    # 高层语义
                "subtasks": {...},   # 各子任务原始结果
                "state": {...}       # 步骤级状态字典
            }
        """
        ts = float(timestamp or time.time())

        # 1. 执行所有子任务（可并行或串行）
        subtask_results = self._run_subtasks(frame, ts, context)

        # 2. 生成聚合后的高层 message
        message = self.build_message(ts, subtask_results, context)

        # 2.5 更新步骤级状态字典（由子类实现具体规则）
        self._state = self.update_state(ts, subtask_results, message, context)

        aggregated: JsonDict = {
            "timestamp": ts,
            "task_name": self._name,
            "message": message,
            "subtasks": subtask_results,
            "state": self._state,
        }

        # 3. 写入 rt / ca 两个 cache
        self._push_cache(self._rt_cache_msg, aggregated)
        self._push_cache(self._ca_cache_msg, aggregated)

        return aggregated

    # ---- 子类可定制部分 ----

    def _run_subtasks(
        self,
        frame: np.ndarray,
        timestamp: float,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, JsonDict]:
        """执行所有子任务，默认策略：
        - 若设置 parallel 且存在 executor，则并行执行
        - 否则按顺序串行执行

        子类如需更精细的调度（如分批并行、多阶段流水线），可覆写本方法。
        """

        prev_stage_cache: Dict[str, Any] = {}
        results: Dict[str, JsonDict] = {}

        if self._parallel and self._executor is not None and len(self._subtasks) > 1:
            # 并行执行：上一阶段 cache 统一使用当前已聚合的 prev_stage_cache
            futures: Dict[Future, SubtaskPipelineBase] = {}
            for st in self._subtasks:
                fut = self._executor.submit(
                    st.infer_frame,
                    frame,
                    timestamp,
                    prev_stage_cache,
                )
                futures[fut] = st

            for fut, st in futures.items():
                try:
                    res = fut.result()
                except Exception as e:  # pragma: no cover - 运行时保护
                    res = {"success": False, "error": str(e), "task_name": st.name, "timestamp": timestamp}
                results[st.name] = res
        else:
            # 串行执行：每个子任务都能看到前面子任务的聚合结果
            for st in self._subtasks:
                try:
                    res = st.infer_frame(frame, timestamp, prev_stage_cache)
                except Exception as e:  # pragma: no cover - 运行时保护
                    res = {"success": False, "error": str(e), "task_name": st.name, "timestamp": timestamp}
                results[st.name] = res
                # 更新上一阶段缓存视图：这里简单按 name -> result 聚合
                prev_stage_cache[st.name] = res

        return results

    @abstractmethod
    def build_message(
        self,
        timestamp: float,
        subtask_results: Mapping[str, JsonDict],
        context: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """根据所有子任务的结果构建高层语义 message。

        例如：
        - 综合 bending / bubble / motion 结果生成 alerts 列表
        - 生成业务需要的状态机输出

        返回值必须是 JSON 可序列化的 dict。
        """

    @abstractmethod
    def update_state(
        self,
        timestamp: float,
        subtask_results: Mapping[str, JsonDict],
        message: Mapping[str, Any],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """根据子任务结果与聚合 message 更新步骤状态字典。

        设计目的：
        - 将“步骤是否完成”“进度估计”等逻辑下沉到 TaskPipeline 的子类中；
        - 基类只负责在 infer_frame 中调用该方法，并持久化到 self._state；
        - 主进程可以通过 state / is_step_completed() 做流程控制，而无需关心细节。

        要求：
        - 返回一个新的 state dict，基类会用其覆盖 self._state；
        - 建议至少包含布尔字段 ``step_completed``，用于标记步骤是否完成。
        """

    # ---- 异步聚合钩子（可选覆写） ----

    def _visualize_and_update_state(
        self,
        subtask_pos_latest: Mapping[str, Optional[JsonDict]],
        subtask_msg_latest: Mapping[str, Optional[JsonDict]],
    ) -> None:
        """默认异步聚合钩子，供后台线程周期性调用。

        基类提供空实现，子类可根据需要覆写本方法，在其中：
        - 利用 subtask_pos_latest / subtask_msg_latest 进行可视化渲染；
        - 将生成的 FrameData 写入 self.rt_cache_frame / self.ca_cache_frame；
        - 根据聚合结果更新 self._state；
        - （可选）向 self._rt_cache_msg / self._ca_cache_msg 追加聚合后的消息。

        注意：该方法在后台线程中执行，实现时应避免长时间阻塞。
        """

    # ---- 工具方法 ----

    @staticmethod
    def _push_cache(cache: Deque[JsonDict], item: JsonDict, maxlen: Optional[int] = None) -> None:
        cache.append(item)
        # 若外部 deque 已设置 maxlen，这里无需再裁剪；但为了安全可选支持手动 maxlen
        if maxlen is not None:
            while len(cache) > maxlen:
                cache.popleft()
