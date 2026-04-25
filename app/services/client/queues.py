"""
客户端队列管理
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, TYPE_CHECKING

import numpy as np

from app.models.frame import FrameData
from app.models.task import Task as CleaningTask
from app.services.inference.data_models import DetectionOutput, get_task_metric_map

if TYPE_CHECKING:
    from app.services.inference.models import AlarmRecord, InferenceResult


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
    """

    def __init__(
        self,
        client_id: str = "",
        ca_segment_len: int = 150,
        ca_maxlen: int = 2700,
        resize_width: int = 640,
        resize_height: int = 480,
        inference_fps: int = 15,
        initial_stage: str = "LEAK",
    ):
        # 客户端标识
        self.client_id = client_id

        # 尺寸配置
        self.resize_width = resize_width
        self.resize_height = resize_height

        # 推理帧率配置
        self.inference_fps = inference_fps
        self.last_inference_timestamp: float = 0.0

        # 线程锁（保护时间戳更新和最新帧访问）
        self._lock = threading.Lock()

        # 最新原始帧缓存（用于异步聚合可视化）
        self.latest_raw_frame: Optional[np.ndarray] = None
        self.latest_raw_timestamp: float = (
            time.time()
        )  # 初始化为创建时间，支持启动失败检测

        # CA-ReadyQueue: 等待推理的原始帧（设置最大长度限制防止溢出）
        self.ca_ready: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # CA-RawQueue: 原始帧副本，用于落盘生成原始视频（设置最大长度限制）
        self.ca_raw: Deque[FrameData] = deque(maxlen=ca_maxlen)
        self.frames_dropped_raw: int = 0  # ca_raw 因 deque 溢出被淘汰的帧数（可观测）
        # CA-ProcessedQueue: 处理后的帧，用于生成 HLS（设置最大长度限制）
        self.ca_processed: Deque[FrameData] = deque(maxlen=ca_maxlen)
        self.ca_segment_len = ca_segment_len
        self.task: Optional[CleaningTask] = None  # 关联的清洗任务

        # 最新渲染帧（单槽位，供前端 WebSocket 实时推流）
        self._latest_rendered: Optional[FrameData] = None
        self._rendered_lock = threading.Lock()

        # 最新推理结果原子快照（由 InferenceLoop 写入，VisualizationWorker 读取）
        self._latest_inference: Optional[InferenceResult] = None
        self._inference_lock = threading.Lock()

        # 当前处理阶段（LEAK / CLEAN / 等）
        self._stage: str = initial_stage
        self._stage_lock = threading.Lock()

        # 分段落盘：写满一段时直接调用 persistence_manager（延迟 import 避免加载顺序问题）

        # --- Temporal 解耦：slide_window + latest_temporal + alarm_log ---
        # 滑动窗口：per-task 的 DetectionOutput 环形缓冲，约 5s（利用 DetectionOutput.timestamp）
        self._slide_window: Dict[str, Deque[DetectionOutput]] = {}
        self._slide_window_seconds: float = 10.0
        self._slide_window_lock = threading.Lock()

        # 最新时序事件列表（由 TemporalWorker 写入，前端读取）
        self._latest_temporal: List[str] = []
        self._temporal_lock = threading.Lock()

        # 告警日志：最近 N 条告警记录（内存环形缓冲区，供前端展示）
        self._alarm_log: Deque[AlarmRecord] = deque(maxlen=100)
        self._alarm_seq: int = 0
        self._alarm_log_lock = threading.Lock()

        # 告警 gate：固定冷却窗口，key=(task_id:metric:mode)，5s 内只允许通过一次
        self._alarm_gate: Dict[str, float] = {}   # gate_key -> last_passed_ts
        self._alarm_gate_window: float = 5.0
        self._alarm_gate_lock = threading.Lock()

    # --- 封装操作方法，减少外部直接操作队列 ---
    def append_ca_ready_with_throttle(self, frame_data: FrameData) -> bool:
        """
        添加帧到待推理队列（带帧率限制）
        用于拉流时降频写入，避免频繁推理

        Args:
            frame_data: 帧数据

        Returns:
            True 表示写入成功，False 表示跳过（帧率限制或队列满）
        """
        with self._lock:
            # 帧率控制：间隔 = 1.0 / inference_fps
            current_time = time.time()
            interval = 1.0 / self.inference_fps

            if current_time - self.last_inference_timestamp < interval:
                return False  # 跳过，不写入

            # 检查队列是否满（maxlen 可能为 None）
            max_len = self.ca_ready.maxlen
            if max_len is not None and len(self.ca_ready) >= max_len:
                return False  # 队列满，丢弃

            self.ca_ready.append(frame_data)
            self.last_inference_timestamp = current_time
            return True

    def append_ca_raw(self, frame_data: FrameData) -> bool:
        """
        添加原始帧到落盘队列，同时更新最新原始帧缓存。
        若积累帧数达到 ca_segment_len 且已绑定任务，直接触发持久化。

        Returns:
            True 表示成功，False 表示异常
        """
        try:
            frames_to_persist = None
            task_id = None
            with self._lock:
                if self.ca_raw.maxlen is not None and len(self.ca_raw) >= self.ca_raw.maxlen:
                    self.frames_dropped_raw += 1
                self.ca_raw.append(frame_data)
                self.latest_raw_frame = frame_data.frame
                self.latest_raw_timestamp = frame_data.timestamp
                if self.task is not None and len(self.ca_raw) >= self.ca_segment_len:
                    task_id = self.get_task_id()
                    frames_to_persist = self.pop_n_ca_raw(self.ca_segment_len)

            if frames_to_persist is not None and task_id is not None:
                from app.services.persistence import persistence_manager
                persistence_manager.persist_hls_segment(
                    client_id=self.client_id,
                    task_id=task_id,
                    segment_type="raw",
                    frames=frames_to_persist,
                )
            return True
        except Exception:
            return False

    def append_ca_processed(self, frame_data: FrameData) -> None:
        """
        添加处理帧到落盘队列。
        若积累帧数达到 ca_segment_len 且已绑定任务，直接触发持久化。
        """
        frames_to_persist = None
        task_id = None
        with self._lock:
            self.ca_processed.append(frame_data)
            if self.task is not None and len(self.ca_processed) >= self.ca_segment_len:
                task_id = self.get_task_id()
                frames_to_persist = self.pop_n_ca_processed(self.ca_segment_len)

        if frames_to_persist is not None and task_id is not None:
            from app.services.persistence import persistence_manager
            persistence_manager.persist_hls_segment(
                client_id=self.client_id,
                task_id=task_id,
                segment_type="processed",
                frames=frames_to_persist,
            )

    def set_latest_rendered(self, frame_data: Optional[FrameData]) -> None:
        """更新最新渲染帧（由 VisualizationWorker 调用）。传 None 表示清空。"""
        with self._rendered_lock:
            self._latest_rendered = frame_data

    def get_latest_rendered(self) -> Optional[FrameData]:
        """获取最新渲染帧（由 WebSocket 前端推流调用）。"""
        with self._rendered_lock:
            return self._latest_rendered

    def get_latest_result(self) -> Optional[FrameData]:
        """返回最新的实时渲染结果。"""
        return self.get_latest_rendered()

    # --- latest_inference 操作（原子推理快照） ---

    def set_latest_inference(self, result: "InferenceResult") -> None:
        """原子写入最新推理结果（由 InferenceLoop 调用）。

        保留完整的 {task_name: DetectionOutput} 捆绑，
        保证同一帧所有 task 结果的一致性。
        """
        with self._inference_lock:
            self._latest_inference = result

    def get_latest_inference(self) -> Optional["InferenceResult"]:
        """原子读取最新推理结果（由 VisualizationWorker 调用）。"""
        with self._inference_lock:
            return self._latest_inference

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """获取最新原始帧（用于可视化）。

        由于推理降帧，可视化需要使用最新的原始帧而非推理时的旧帧。

        Returns:
            最新原始帧（np.ndarray）或 None（如果没有帧）
        """
        with self._lock:
            if self.latest_raw_frame is not None:
                return self.latest_raw_frame.copy()
            return None

    def get_task_id(self) -> Optional[int]:
        return self.task.task_id if self.task else None

    def to_status_dict(self) -> dict:
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "has_rendered": self._latest_rendered is not None,
        }

    def get_queue_depths(self) -> dict:
        """
        获取队列深度统计（新增方法，与 to_status_dict 类似但更语义化）

        Returns:
            包含各队列长度的字典
        """
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "has_rendered": self._latest_rendered is not None,
        }

    def get_ca_ready_capacity(self) -> int:
        """获取 ca_ready 队列的最大容量

        Returns:
            队列最大长度，如果未设置则返回 0
        """
        return self.ca_ready.maxlen or 0

    def get_ca_raw_capacity(self) -> int:
        """获取 ca_raw 队列的最大容量

        Returns:
            队列最大长度，如果未设置则返回 0
        """
        return self.ca_raw.maxlen or 0

    def get_ca_raw_length(self) -> int:
        """获取 ca_raw 队列的当前长度

        Returns:
            队列当前帧数
        """
        return len(self.ca_raw)

    def get_ca_processed_length(self) -> int:
        """获取 ca_processed 队列的当前长度

        Returns:
            队列当前帧数
        """
        return len(self.ca_processed)

    def drain_ca_raw(self) -> List[FrameData]:
        """原子排空 ca_raw 队列（线程安全，供 flush 使用）"""
        with self._lock:
            frames = list(self.ca_raw)
            self.ca_raw.clear()
            return frames

    def drain_ca_processed(self) -> List[FrameData]:
        """原子排空 ca_processed 队列（线程安全，供 flush 使用）"""
        with self._lock:
            frames = list(self.ca_processed)
            self.ca_processed.clear()
            return frames

    def pop_n_ca_raw(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_raw))):
            out.append(self.ca_raw.popleft())
        return out

    def pop_n_ca_processed(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_processed))):
            out.append(self.ca_processed.popleft())
        return out

    def clear(self) -> None:
        self.ca_ready.clear()
        self.ca_raw.clear()
        self.ca_processed.clear()
        self.latest_raw_frame = None
        self.latest_raw_timestamp = time.time()
        self.task = None
        with self._rendered_lock:
            self._latest_rendered = None
        with self._inference_lock:
            self._latest_inference = None
        with self._slide_window_lock:
            self._slide_window.clear()
        with self._temporal_lock:
            self._latest_temporal = []
        with self._alarm_log_lock:
            self._alarm_log.clear()
        with self._alarm_gate_lock:
            self._alarm_gate.clear()

    def pop_ca_ready(self) -> Optional[FrameData]:
        """从推理队列弹出一帧（FIFO）
        
        Returns:
            FrameData 或 None（队列为空）
        """
        return self.ca_ready.popleft() if self.ca_ready else None

    def set_task(self, task: Optional[CleaningTask]) -> None:
        old_task_id = self.get_task_id()
        self.task = task
        self.task_started_at: float = time.time() if task is not None else 0.0
        new_task_id = self.get_task_id()

        # Task changed => reset task-scoped realtime caches.
        if old_task_id != new_task_id:
            with self._slide_window_lock:
                self._slide_window.clear()
            with self._temporal_lock:
                self._latest_temporal = []
            with self._alarm_log_lock:
                self._alarm_log.clear()
                self._alarm_seq = 0
            with self._alarm_gate_lock:
                self._alarm_gate.clear()

    def get_task(self) -> Optional[CleaningTask]:
        return self.task

    # --- 阶段管理 ---

    def get_stage(self) -> str:
        """获取当前处理阶段。"""
        with self._stage_lock:
            return self._stage

    def set_stage(self, stage: str) -> None:
        """设置当前处理阶段。"""
        with self._stage_lock:
            self._stage = stage

    # --- slide_window 操作 ---

    def push_detection(self, task_name: str, output: DetectionOutput) -> None:
        """将 DetectionOutput 追加到 per-task 滑动窗口，自动淘汰过期条目。

        由 InferenceLoop._write_back_results() 调用（轻量同步）。
        利用 DetectionOutput.timestamp 进行窗口淘汰。
        """
        with self._slide_window_lock:
            if task_name not in self._slide_window:
                self._slide_window[task_name] = deque()
            window = self._slide_window[task_name]
            window.append(output)
            # 淘汰过期条目
            cutoff = output.timestamp - self._slide_window_seconds
            while window and window[0].timestamp < cutoff:
                window.popleft()

    def get_slide_window(self, task_name: str) -> List[DetectionOutput]:
        """返回指定 task 滑动窗口的快照副本（线程安全）。"""
        with self._slide_window_lock:
            window = self._slide_window.get(task_name)
            if not window:
                return []
            return list(window)

    def get_slide_window_latest(self, task_name: str) -> Optional[DetectionOutput]:
        """返回指定 task 滑动窗口的最新条目。"""
        with self._slide_window_lock:
            window = self._slide_window.get(task_name)
            if not window:
                return None
            return window[-1]

    # --- latest_temporal 操作 ---

    def set_latest_temporal(self, events: List[str]) -> None:
        """覆写最新时序事件列表（由 TemporalWorker 调用）。"""
        with self._temporal_lock:
            self._latest_temporal = events

    def get_latest_temporal(self) -> List[str]:
        """读取最新时序事件列表。"""
        with self._temporal_lock:
            return list(self._latest_temporal)

    # --- alarm_log 操作 ---

    def try_pass_alarm_gate(self, task_id: Optional[int], metric: str, mode: str) -> bool:
        """固定冷却窗口限流（5s）：True = 通过，False = 丢弃。key=(task_id, metric, mode)。

        计时器仅在告警通过时重置，被拦截的告警不续期窗口。
        """
        gate_key = f"{task_id}:{metric}:{mode}"
        now = time.time()
        with self._alarm_gate_lock:
            last = self._alarm_gate.get(gate_key)
            if last is not None and (now - last) < self._alarm_gate_window:
                return False          # 拦截；不更新 last，窗口不续期
            self._alarm_gate[gate_key] = now   # 仅通过时重置计时器
            return True

    def append_alarm_record(self, record: AlarmRecord) -> None:
        """直接追加告警到内存环形日志；调用方须先通过 try_pass_alarm_gate。"""
        with self._alarm_log_lock:
            self._alarm_seq += 1
            record.seq = self._alarm_seq
            self._alarm_log.append(record)

    def get_recent_alarms(self, n: int = 10) -> List[AlarmRecord]:
        """返回最近 n 条告警记录（最新在后）。"""
        with self._alarm_log_lock:
            items = list(self._alarm_log)
        return items[-n:]

    def get_alarm_increment(self, since_seq: int = 0) -> List[AlarmRecord]:
        """Return alarms with seq > since_seq."""
        with self._alarm_log_lock:
            items = [a for a in self._alarm_log if a.seq > since_seq]
        return items

    def get_alarm_max_seq(self) -> int:
        with self._alarm_log_lock:
            return self._alarm_seq

    def get_signals_10s(self) -> Dict[str, Dict[str, Any]]:
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
        alarms = self.get_alarm_increment(since_seq=since_seq)
        return {
            "task_id": task_id,
            "max_seq": self.get_alarm_max_seq(),
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

        # 从 slide_window 提取各 task 最新检测状态
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
