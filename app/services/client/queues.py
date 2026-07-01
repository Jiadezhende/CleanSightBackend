"""
客户端队列管理
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, TYPE_CHECKING

import numpy as np

from app.domain.alarm import Alarm
from app.domain.detection import FrameDetections
from app.domain.frame import Frame
from app.domain.task import CleaningTask

if TYPE_CHECKING:
    from app.services.inference.models import FrameInference


class ClientQueues:
    """
    架构说明：
    - CA-ReadyQueue: 从 RTMP 提取的原始帧，等待推理（设置最大长度防止溢出）
    - CA-RawQueue: 原始帧副本，用于生成原始视频 HLS 段（设置最大长度防止溢出）
    - CA-ProcessedQueue: 推理后的处理帧（含标注），用于生成处理后 HLS 段（设置最大长度防止溢出）
    - _latest_rendered: 单槽位，最新渲染帧，供前端 WebSocket 实时推流
    - _latest_inference: 单槽位，最新推理结果原子快照，供 VisualizationWorker 读取

    内存保护：
    - 所有队列都设置了 maxlen 限制，当队列满时自动丢弃最旧的帧
    - 默认 CA 队列最大长度为 2700 帧，约 90 秒的视频缓存（30fps）

    锁清单（Lock Inventory）：
      _task_lock        Lock   self.task + self.task_started_at（读多写少共享资源）
      ca_ready          无锁   SPSC deque：单生产者 decoder / 单消费者 dispatcher，GIL 保证原子性
      _raw_lock         Lock   ca_raw + latest_raw_frame + latest_raw_timestamp
      _viz_lock         Lock   ca_processed + _latest_rendered（VizWorker 对同帧连续写两者）
      _inference_lock   Lock   _latest_inference（原子推理快照槽）
      _frontend_lock    Lock   _stage + _latest_temporal（前端状态，合并低频写）
      _slide_window_lock Lock  _slide_window dict（推理每帧写入，最高竞争锁）
      _alarm_lock       Lock   _alarm_log + _alarm_seq + _alarm_gate（告警生命周期）

    全清顺序（clear() 同时持锁时的固定顺序，防死锁）：
      _task_lock → _raw_lock → _viz_lock → _inference_lock
      → _frontend_lock → _slide_window_lock → _alarm_lock

    热路径读 self.task 模式：进入 frame lock 前先调 get_task() 快照，两把锁永不嵌套。
    """

    def __init__(
        self,
        client_id: str = "",
        ca_segment_len: int = 150,
        ca_maxlen: int = 2700,
        resize_width: int = 640,
        resize_height: int = 480,
        inference_fps: int = 15,
        raw_fps: int = 30,
        # 默认 MOCK：未分配任务的客户端走透传，不跑真实检测/告警。
        # 真实 stage 由 InferenceManager.set_task 按 step_id 赋值。
        initial_stage: str = "MOCK",
    ):
        # 客户端标识
        self.client_id = client_id

        # 尺寸配置
        self.resize_width = resize_width
        self.resize_height = resize_height

        # 帧率配置（降采样率 = inference_fps / raw_fps）
        self.inference_fps = inference_fps
        self.raw_fps = raw_fps
        # 抽帧相位累加器（Bresenham 均匀抽帧）：仅由 decoder 线程读写
        # （append_ca_ready_with_throttle 内部），无并发，不加锁
        self._decimate_phase: int = 0

        # --- 锁声明（顺序同 Lock Inventory 全清顺序）---
        self._task_lock = threading.Lock()       # self.task + self.task_started_at
        self._raw_lock = threading.Lock()        # ca_raw + 帧缓存
        self._viz_lock = threading.Lock()        # ca_processed + _latest_rendered
        self._inference_lock = threading.Lock()  # _latest_inference
        self._frontend_lock = threading.Lock()   # _stage + _latest_temporal
        self._slide_window_lock = threading.Lock()
        self._alarm_lock = threading.Lock()      # _alarm_log + _alarm_seq + _alarm_gate

        # 任务绑定（由 _task_lock 保护）
        self.task: Optional[CleaningTask] = None
        self.task_started_at: float = 0.0
        self._initial_stage = initial_stage  # 供 clear() 重置

        # 最新原始帧缓存（由 _raw_lock 保护）
        self.latest_raw_frame: Optional[np.ndarray] = None
        self.latest_raw_timestamp: float = (
            time.time()
        )  # 初始化为创建时间，支持启动失败检测

        # CA-ReadyQueue：无锁 SPSC deque（单生产者 decoder / 单消费者 dispatcher）
        self.ca_ready: Deque[Frame] = deque(maxlen=ca_maxlen)
        # CA-RawQueue（由 _raw_lock 保护）
        self.ca_raw: Deque[Frame] = deque(maxlen=ca_maxlen)
        self.frames_dropped_raw: int = 0
        # CA-ProcessedQueue（由 _viz_lock 保护）
        self.ca_processed: Deque[Frame] = deque(maxlen=ca_maxlen)
        self.frames_dropped_processed: int = 0
        self.ca_segment_len = ca_segment_len

        # 最新渲染帧（单槽位，由 _viz_lock 保护，供前端 WebSocket 实时推流）
        self._latest_rendered: Optional[Frame] = None

        # 最新推理结果原子快照（由 _inference_lock 保护）
        self._latest_inference: Optional[FrameInference] = None

        # 当前处理阶段（由 _frontend_lock 保护）
        self._stage: str = initial_stage

        # 滑动窗口：per-stream(detector.name) 检测环形缓冲（由 _slide_window_lock 保护）
        self._slide_window: Dict[str, Deque[FrameDetections]] = {}
        # 缓冲保留时长底线（保证 signals_10s 仍见 10s）；感受野只向上扩展，不缩短。
        self._slide_window_seconds: float = 10.0
        # per-stream 感受野覆盖：{流名: 订阅该流的算子最大 window_seconds}，由 set_stream_windows 配置。
        # 实际保留时长 = max(底线, 该流感受野)。
        self._stream_windows: Dict[str, float] = {}

        # 最新时序事件列表（由 _frontend_lock 保护，与 _stage 合并）
        self._latest_temporal: List[str] = []

        # 告警日志（由 _alarm_lock 保护）
        self._alarm_log: Deque[Alarm] = deque(maxlen=100)
        self._alarm_seq: int = 0

        # 告警 gate（由 _alarm_lock 保护，与 _alarm_log 合并）
        self._alarm_gate: Dict[str, float] = {}
        self._alarm_gate_window: float = 5.0

    # --- 封装操作方法 ---

    def append_ca_ready_with_throttle(self, frame_data: Frame) -> bool:
        """
        添加帧到待推理队列（Bresenham 相位累加器均匀抽帧 + 背压）。

        输入为 ffmpeg 规范化后的 CFR raw_fps 流，按 raw_fps→inference_fps 做均匀抽帧：
        每个输入帧累加 inference_fps，跨过 raw_fps 阈值时放行一帧。长期保留率精确
        = inference_fps/raw_fps（支持非整除比，如 30→20 取 keep-keep-drop），不依赖
        wall-clock —— 消除解码线程调度抖动导致的真实率漂移（旧墙钟门把 15 漏成 ~12）。

        ca_ready 为无锁 SPSC deque，decoder 是唯一写入方，dispatcher 是唯一消费方。
        _decimate_phase 仅由本方法（decoder 线程）读写，无并发，不加锁。
        """
        # 1. 相位累加器选帧：相位每输入帧推进一次（累加器正确性的不变式），
        #    跨过 raw_fps 阈值才放行。
        self._decimate_phase += self.inference_fps
        if self._decimate_phase < self.raw_fps:
            return False
        self._decimate_phase -= self.raw_fps

        # 2. 背压：仅对选中帧生效——推理队列满则丢（过载语义同旧）。
        max_len = self.ca_ready.maxlen
        if max_len is not None and len(self.ca_ready) >= max_len:
            return False

        self.ca_ready.append(frame_data)
        return True

    @staticmethod
    def _resolve_step_id(task: Optional[CleaningTask]) -> Optional[int]:
        """从 task.current_step 解析 step_id；非法返回 None（拒绝落盘）。"""
        if task is None or task.current_step is None:
            return None
        try:
            return int(task.current_step)
        except (TypeError, ValueError):
            return None

    def append_ca_raw(self, frame_data: Frame) -> bool:
        """
        添加原始帧到落盘队列，同时更新最新原始帧缓存。
        若积累帧数达到 ca_segment_len 且已绑定任务，直接触发持久化。

        self.task 在进入 _raw_lock 前快照，避免 _raw_lock 与 _task_lock 嵌套。
        """
        try:
            # 快照 task（在 frame lock 外，两锁永不嵌套）
            _task = self.get_task()

            frames_to_persist = None
            with self._raw_lock:
                if self.ca_raw.maxlen is not None and len(self.ca_raw) >= self.ca_raw.maxlen:
                    self.frames_dropped_raw += 1
                self.ca_raw.append(frame_data)
                self.latest_raw_frame = frame_data.frame
                self.latest_raw_timestamp = frame_data.timestamp
                if _task is not None and len(self.ca_raw) >= self.ca_segment_len:
                    frames_to_persist = self.pop_n_ca_raw(self.ca_segment_len)

            if frames_to_persist is not None and _task is not None:
                step_id = self._resolve_step_id(_task)
                if step_id is None:
                    import logging
                    logging.getLogger(__name__).error(
                        "[append_ca_raw] invalid current_step=%r, skip persistence (task_id=%s)",
                        _task.current_step, _task.task_id,
                    )
                    return False
                from app.services.persistence import persistence_manager
                persistence_manager.persist_hls_segment(
                    task_id=_task.task_id,
                    step_id=step_id,
                    segment_type="raw",
                    frames=frames_to_persist,
                )
            return True
        except Exception:
            return False

    def append_ca_processed(self, frame_data: Frame) -> None:
        """
        添加处理帧到落盘队列。
        若积累帧数达到 ca_segment_len 且已绑定任务，直接触发持久化。

        self.task 在进入 _viz_lock 前快照，避免 _viz_lock 与 _task_lock 嵌套。
        """
        _task = self.get_task()

        frames_to_persist = None
        with self._viz_lock:
            if (
                self.ca_processed.maxlen is not None
                and len(self.ca_processed) >= self.ca_processed.maxlen
            ):
                self.frames_dropped_processed += 1
            self.ca_processed.append(frame_data)
            if _task is not None and len(self.ca_processed) >= self.ca_segment_len:
                frames_to_persist = self.pop_n_ca_processed(self.ca_segment_len)

        if frames_to_persist is not None and _task is not None:
            step_id = self._resolve_step_id(_task)
            if step_id is None:
                import logging
                logging.getLogger(__name__).error(
                    "[append_ca_processed] invalid current_step=%r, skip persistence (task_id=%s)",
                    _task.current_step, _task.task_id,
                )
                return
            from app.services.persistence import persistence_manager
            persistence_manager.persist_hls_segment(
                task_id=_task.task_id,
                step_id=step_id,
                segment_type="processed",
                frames=frames_to_persist,
            )

    def set_latest_rendered(self, frame_data: Optional[Frame]) -> None:
        """更新最新渲染帧（由 VisualizationWorker 调用）。传 None 表示清空。"""
        with self._viz_lock:
            self._latest_rendered = frame_data

    def get_latest_rendered(self) -> Optional[Frame]:
        """获取最新渲染帧（由 WebSocket 前端推流调用）。"""
        with self._viz_lock:
            return self._latest_rendered

    def get_latest_result(self) -> Optional[Frame]:
        """返回最新的实时渲染结果。"""
        return self.get_latest_rendered()

    # --- latest_inference 操作（原子推理快照）---

    def set_latest_inference(self, result: "FrameInference") -> None:
        """原子写入最新推理结果（由 InferenceLoop 调用）。"""
        with self._inference_lock:
            self._latest_inference = result

    def get_latest_inference(self) -> Optional["FrameInference"]:
        """原子读取最新推理结果（由 VisualizationWorker 调用）。"""
        with self._inference_lock:
            return self._latest_inference

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """获取最新原始帧（用于可视化）。"""
        with self._raw_lock:
            if self.latest_raw_frame is not None:
                return self.latest_raw_frame.copy()
            return None

    def get_task(self) -> Optional[CleaningTask]:
        with self._task_lock:
            return self.task

    def get_task_id(self) -> Optional[int]:
        with self._task_lock:
            return self.task.task_id if self.task else None

    def get_step_id(self) -> Optional[int]:
        """解析当前 step_id（落盘目录键，与 HLS 同源）。非法/未绑定返回 None。"""
        return self._resolve_step_id(self.get_task())

    def step_id_of(self, task: Optional[CleaningTask]) -> Optional[int]:
        """从已快照的 task 解析 step_id（与 get_step_id 同口径）。

        供调用方先 get_task() 取一次、再派生 task_id/step_id，避免两次独立读
        之间发生 set_task 导致 (task_id, step_id) 键错配。
        """
        return self._resolve_step_id(task)

    def to_status_dict(self) -> dict:
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "has_rendered": self._latest_rendered is not None,
        }

    def get_queue_depths(self) -> dict:
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "has_rendered": self._latest_rendered is not None,
        }

    def get_ca_ready_capacity(self) -> int:
        return self.ca_ready.maxlen or 0

    def get_ca_raw_capacity(self) -> int:
        return self.ca_raw.maxlen or 0

    def get_ca_processed_capacity(self) -> int:
        return self.ca_processed.maxlen or 0

    def get_ca_raw_length(self) -> int:
        return len(self.ca_raw)

    def get_ca_processed_length(self) -> int:
        return len(self.ca_processed)

    def drain_ca_raw(self) -> List[Frame]:
        """原子排空 ca_raw 队列（线程安全，供 flush 使用）"""
        with self._raw_lock:
            frames = list(self.ca_raw)
            self.ca_raw.clear()
            return frames

    def drain_ca_processed(self) -> List[Frame]:
        """原子排空 ca_processed 队列（线程安全，供 flush 使用）"""
        with self._viz_lock:
            frames = list(self.ca_processed)
            self.ca_processed.clear()
            return frames

    def pop_n_ca_raw(self, n: int) -> List[Frame]:
        out: List[Frame] = []
        for _ in range(min(n, len(self.ca_raw))):
            out.append(self.ca_raw.popleft())
        return out

    def pop_n_ca_processed(self, n: int) -> List[Frame]:
        out: List[Frame] = []
        for _ in range(min(n, len(self.ca_processed))):
            out.append(self.ca_processed.popleft())
        return out

    def clear(self) -> None:
        """原子清除所有队列和缓存（按全清顺序持所有锁）。"""
        locks = [
            self._task_lock, self._raw_lock, self._viz_lock,
            self._inference_lock, self._frontend_lock,
            self._slide_window_lock, self._alarm_lock,
        ]
        with contextlib.ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock)
            self.ca_ready.clear()
            self.ca_raw.clear()
            self.ca_processed.clear()
            self.latest_raw_frame = None
            self.latest_raw_timestamp = time.time()
            self.task = None
            self.task_started_at = 0.0
            self._latest_rendered = None
            self._latest_inference = None
            self._stage = self._initial_stage
            self._latest_temporal = []
            self._slide_window.clear()
            self._alarm_log.clear()
            self._alarm_seq = 0
            self._alarm_gate.clear()

    def pop_ca_ready(self) -> Optional[Frame]:
        """从推理队列弹出一帧（FIFO，无锁 SPSC）"""
        return self.ca_ready.popleft() if self.ca_ready else None

    def set_task(self, task: Optional[CleaningTask]) -> None:
        """线程安全的纯字段赋值。不含缓存清理逻辑，清理由 clear_task_caches() 负责。"""
        with self._task_lock:
            self.task = task
            self.task_started_at = time.time() if task is not None else 0.0

    def clear_task_caches(self) -> None:
        """清空任务级别实时分析缓存（slide_window / temporal / alarm）。

        调用方须确保旧 TemporalActor 已通过 finalize_and_stop() 停止，
        避免旧 Actor 的 settlement 写入落入已清空的缓存。
        """
        with self._slide_window_lock:
            self._slide_window.clear()
        with self._frontend_lock:
            self._latest_temporal = []
        with self._alarm_lock:
            self._alarm_log.clear()
            self._alarm_seq = 0
            self._alarm_gate.clear()

    # --- 阶段管理 ---

    def get_stage(self) -> str:
        """获取当前处理阶段。"""
        with self._frontend_lock:
            return self._stage

    def set_stage(self, stage: str) -> None:
        """设置当前处理阶段。"""
        with self._frontend_lock:
            self._stage = stage

    # --- slide_window 操作 ---

    def set_stream_windows(self, windows: Dict[str, float]) -> None:
        """配置 per-stream 感受野（{流名: 最大 window_seconds}），整体替换。

        由 InferenceManager.set_task() 在算子实例化后调用：缓冲保留时长取
        max(底线 10s, 该流感受野)，故感受野只向上扩展，signals_10s 的 10s 不受影响。
        """
        with self._slide_window_lock:
            self._stream_windows = dict(windows)

    def push_detection(self, task_name: str, output: FrameDetections) -> None:
        """将 FrameDetections 追加到 per-stream 滑动窗口，按感受野自动淘汰过期条目。"""
        with self._slide_window_lock:
            if task_name not in self._slide_window:
                self._slide_window[task_name] = deque()
            window = self._slide_window[task_name]
            window.append(output)
            retain = max(
                self._slide_window_seconds, self._stream_windows.get(task_name, 0.0)
            )
            cutoff = output.timestamp - retain
            while window and window[0].timestamp < cutoff:
                window.popleft()

    def get_slide_window(self, task_name: str) -> List[FrameDetections]:
        """返回指定 task 滑动窗口的快照副本（线程安全）。"""
        with self._slide_window_lock:
            window = self._slide_window.get(task_name)
            if not window:
                return []
            return list(window)

    def get_slide_window_latest(self, task_name: str) -> Optional[FrameDetections]:
        """返回指定 task 滑动窗口的最新条目。"""
        with self._slide_window_lock:
            window = self._slide_window.get(task_name)
            if not window:
                return None
            return window[-1]

    # --- latest_temporal 操作 ---

    def set_latest_temporal(self, events: List[str]) -> None:
        """覆写最新时序事件列表（由 TemporalWorker 调用）。"""
        with self._frontend_lock:
            self._latest_temporal = events

    def get_latest_temporal(self) -> List[str]:
        """读取最新时序事件列表。"""
        with self._frontend_lock:
            return list(self._latest_temporal)

    # --- alarm_log 操作 ---

    def append_alarm_record_with_gate(
        self, task_id: Optional[int], alarm: Alarm, mode: str
    ) -> bool:
        """闸门去重 + 入环形日志，单 _alarm_lock 内原子完成。

        True = 已记录（赋 seq 并入日志），False = 被冷却窗口（5s）拦截、未记录。
        闸门按 (task_id, alarm.metric, mode) 限流；通过后才赋 seq、append。

        task_id 须由调用方在锁外先 get_task_id() 取好传入，不在持 _alarm_lock 时
        反向获取 _task_lock（违反全清顺序，死锁风险）。
        """
        gate_key = f"{task_id}:{alarm.metric}:{mode}"
        now = time.time()
        with self._alarm_lock:
            last = self._alarm_gate.get(gate_key)
            if last is not None and (now - last) < self._alarm_gate_window:
                return False
            self._alarm_gate[gate_key] = now
            self._alarm_seq += 1
            alarm.seq = self._alarm_seq
            self._alarm_log.append(alarm)
            return True

    def get_recent_alarms(self, n: int = 10) -> List[Alarm]:
        """返回最近 n 条告警记录（最新在后）。"""
        with self._alarm_lock:
            items = list(self._alarm_log)
        return items[-n:]

    def get_alarm_increment(self, since_seq: int = 0) -> List[Alarm]:
        """Return alarms with seq > since_seq."""
        with self._alarm_lock:
            items = [a for a in self._alarm_log if a.seq > since_seq]
        return items

    def get_alarm_max_seq(self) -> int:
        with self._alarm_lock:
            return self._alarm_seq

    def get_signals_10s(self) -> Dict[str, Dict[str, Any]]:
        # 懒 import 破除 client ↔ inference 模块级环（naming 是 inference 运行时状态）
        from app.services.inference.naming import get_task_metric_map

        task_metric_map = get_task_metric_map()
        _empty: Dict[str, Any] = {"active": False, "hit_count": 0, "max_conf": 0.0}
        summary: Dict[str, Dict[str, Any]] = {
            m.value: dict(_empty) for m in task_metric_map.values()
        }
        with self._slide_window_lock:
            for task_name, window in self._slide_window.items():
                metric = task_metric_map.get(task_name)
                if metric is None:
                    continue
                hit_count = 0
                max_conf = 0.0
                for output in window:
                    if output.detections:
                        hit_count += 1
                        frame_max_conf = max(d.confidence for d in output.detections)
                        max_conf = max(max_conf, frame_max_conf)
                summary[metric.value] = {
                    "active": hit_count > 0,
                    "hit_count": hit_count,
                    "max_conf": round(float(max_conf), 4),
                }
        return summary

    def get_task_alarm_message(self, task_id: int, since_seq: int = 0) -> Dict[str, Any]:
        # alarm 列表与 max_seq 在同一把锁内读取，保证 max_seq >= max(a.seq for a in alarms)，
        # 避免客户端用推进后的 since_seq 跳过尚未返回的告警。
        # signals_10s 独立持 _slide_window_lock，两路数据本就独立，轻微时差可接受。
        with self._alarm_lock:
            alarms = [a for a in self._alarm_log if a.seq > since_seq]
            max_seq = self._alarm_seq
        return {
            "task_id": task_id,
            "max_seq": max_seq,
            "signals_10s": self.get_signals_10s(),
            "alarms": [
                {
                    "seq": a.seq,
                    "mode": a.mode,
                    "metric": a.metric,
                    "level": a.alarm_level,
                    "message": a.alarm_message,
                    "ts": int(a.timestamp),
                }
                for a in alarms
            ],
        }

    # --- 前端消息打包 ---

    def get_frontend_message(self) -> Dict[str, Any]:
        """打包阶段 + latest_temporal + 检测状态 + 告警，供前端读取。"""
        stage = self.get_stage()
        events = self.get_latest_temporal()
        recent_alarms = self.get_recent_alarms(n=5)

        latest_detections: Dict[str, bool] = {}
        latest_confidences: Dict[str, float] = {}
        with self._slide_window_lock:
            for task_name, window in self._slide_window.items():
                if window:
                    last_output = window[-1]
                    latest_detections[task_name] = len(last_output.detections) > 0
                    if last_output.detections:
                        avg_conf = sum(
                            d.confidence for d in last_output.detections
                        ) / len(last_output.detections)
                    else:
                        avg_conf = 0.0
                    latest_confidences[task_name] = avg_conf

        return {
            "client_id": self.client_id,
            "stage": stage,
            "temporal": {
                "events": events,
            } if events else None,
            "detections": latest_detections,
            "confidences": latest_confidences,
            "recent_alarms": [
                {
                    "alarm_type": a.alarm_type,
                    "alarm_level": a.alarm_level,
                    "alarm_message": a.alarm_message,
                    "timestamp": a.timestamp,
                    "mode": a.mode,
                    "metric": a.metric,
                }
                for a in recent_alarms
            ],
        }
