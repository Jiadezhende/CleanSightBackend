"""
客户端队列管理
"""

from __future__ import annotations

import contextlib
import enum
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, TYPE_CHECKING

import numpy as np

from app.domain.alarm import Alarm
from app.domain.detection import FrameDetections
from app.domain.frame import Frame
from app.utils.metrics import frame_drop_total

if TYPE_CHECKING:
    from app.services.inference.models import FrameInference


class RunState(enum.Enum):
    """一次 run（一个 CQ）的生命周期状态，单调推进 ACTIVE→DRAINING→CLOSED。

    - ACTIVE：正常运行，所有写放行。
    - DRAINING：拆除中，停生产者写（decoder/结果写回/tick），仅放行 settlement 告警 + HLS flush。
    - CLOSED：payload 已释放，一切写被拒；身份小壳仍可读（供 fence/日志）。

    门控在写入**时刻**判 state（非 dispatch 时刻）：迟到写落到 DRAINING/CLOSED 的旧 CQ 被拒，
    不串台到新 run。状态读免锁（枚举原子读 + 单调）；`_state_lock` 仅串行/幂等转换本身，
    绝不与 7 把 payload 锁互嵌——无新锁环。
    """

    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"


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
      _task_lock        Lock   （历史）保留于全清顺序；task/task_id/step_id/stage 现为不可变身份，读免锁
      ca_ready          无锁   SPSC deque：单生产者 decoder / 单消费者 dispatcher，GIL 保证原子性
      _raw_lock         Lock   ca_raw + latest_raw_frame + latest_raw_timestamp
      _viz_lock         Lock   ca_processed + _latest_rendered（VizWorker 对同帧连续写两者）
      _inference_lock   Lock   _latest_inference（原子推理快照槽）
      _frontend_lock    Lock   _latest_temporal（前端时序事件，低频写）
      _slide_window_lock Lock  _slide_window dict（推理每帧写入，最高竞争锁）
      _alarm_lock       Lock   _alarm_log + _alarm_seq + _alarm_gate（告警生命周期）

    全清顺序（clear() 同时持锁时的固定顺序，防死锁）：
      _task_lock → _raw_lock → _viz_lock → _inference_lock
      → _frontend_lock → _slide_window_lock → _alarm_lock

    身份（task_id/step_id/source_ip/stage）为构造定死的不可变 primitive，热路径免锁直读。
    """

    def __init__(
        self,
        ca_segment_len: int = 150,
        ca_maxlen: int = 2700,
        resize_width: int = 640,
        resize_height: int = 480,
        inference_fps: int = 15,
        raw_fps: int = 30,
        *,
        # 不可变运行身份（primitives 直注，一次 CQ == 一次 run，终生不变）。
        # 全默认 None/"" 供纯队列/算子单测裸建；生产由 RunController 传入已解析好的
        # task_id/step_id/stage（int 转换与 stage 解析都在 RunController 边界一次完成）。
        task_id: Optional[int] = None,
        step_id: Optional[int] = None,
        source_ip: str = "",
        stage: str = "MOCK",
    ):
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
        self._task_lock = threading.Lock()       # (历史)保留于全清顺序；身份已不可变、读免锁
        self._raw_lock = threading.Lock()        # ca_raw + 帧缓存
        self._viz_lock = threading.Lock()        # ca_processed + _latest_rendered
        self._inference_lock = threading.Lock()  # _latest_inference
        self._frontend_lock = threading.Lock()   # _latest_temporal
        self._slide_window_lock = threading.Lock()
        self._alarm_lock = threading.Lock()      # _alarm_log + _alarm_seq + _alarm_gate

        # 运行状态机的锁（独立于下方 7 把 payload 锁：只串行/幂等本状态转换，
        # 从不与任何 payload 锁互嵌，故不引入新锁环——见 RunState docstring）
        self._state: RunState = RunState.ACTIVE
        self._state_lock = threading.Lock()

        # 不可变运行身份：一次构造定死，直读、无锁——CQ 经 client_manager COW
        # 换引用发布，读者原子读引用即 acquire，观察不到半建对象。切 step/重启 = 建新 CQ 换槽，
        # 不在此对象上改身份。故 settlement 归属天然正确，无需"先停旧 actor 再切字段"的排序不变式。
        # 注：无 client_id 字段——注册表路由键即 self.task_id(int)；source_ip 为被动来源字段。
        # step_id 为已解析好的 int（DBAlarm.step_id/落盘目录/FeatureStore 分区键全链路 int）；
        # 字符串来源 current_step→int 的转换在 RunController 边界一次完成，本类不再解析。
        self.task_id: Optional[int] = task_id
        self.step_id: Optional[int] = step_id
        self.source_ip: str = source_ip
        self.stage: str = stage
        # run 起始时刻：供 GlobalHealthMonitor 的 task_max_duration 看门狗判定跑飞任务并超时拆除
        # （monitor.py 用 now - task_started_at ≥ task_max_duration 触发 _handle_task_timeout）。
        self.task_started_at: float = time.time() if task_id is not None else 0.0

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
        # 写门：非 ACTIVE（拆除中/已关）拒写——在推进相位累加器之前拒，避免 late 帧扰动抽帧节奏。
        if self._state is not RunState.ACTIVE:
            return False

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

    def append_ca_raw(self, frame_data: Frame) -> bool:
        """
        添加原始帧到落盘缓冲，同时更新最新原始帧缓存（纯缓冲，不触发落盘）。

        分段落盘由 persistence 的 HLSSegmentSweeper 周期 take_raw_segment() 拉取，
        本方法只管入队 + 丢帧计数 + 刷新 latest_raw_frame。返回是否入队（非 ACTIVE 拒）。
        """
        # 写门：非 ACTIVE 拒写（拆除中 raw 也停——残段由 flush_residual_segments 收尾）
        if self._state is not RunState.ACTIVE:
            return False
        with self._raw_lock:
            if self.ca_raw.maxlen is not None and len(self.ca_raw) >= self.ca_raw.maxlen:
                self.frames_dropped_raw += 1
                frame_drop_total.labels(reason="raw_backpressure").inc()
            self.ca_raw.append(frame_data)
            self.latest_raw_frame = frame_data.frame
            self.latest_raw_timestamp = frame_data.timestamp
        return True

    def append_ca_processed(self, frame_data: Frame) -> None:
        """
        添加处理帧到落盘缓冲（纯缓冲，不触发落盘）。

        分段落盘由 persistence 的 HLSSegmentSweeper 周期 take_processed_segment() 拉取，
        本方法只管入队 + 丢帧计数。
        """
        # 写门：非 ACTIVE 拒写
        if self._state is not RunState.ACTIVE:
            return
        with self._viz_lock:
            if (
                self.ca_processed.maxlen is not None
                and len(self.ca_processed) >= self.ca_processed.maxlen
            ):
                self.frames_dropped_processed += 1
                frame_drop_total.labels(reason="hls_backpressure").inc()
            self.ca_processed.append(frame_data)

    def set_latest_rendered(self, frame_data: Optional[Frame]) -> None:
        """更新最新渲染帧（由 VisualizationWorker 调用）。传 None 表示清空。

        写门：非 ACTIVE 拒**非空**写；清空(None)放行——拆除期允许清掉前端残帧。
        """
        if frame_data is not None and self._state is not RunState.ACTIVE:
            return
        with self._viz_lock:
            self._latest_rendered = frame_data

    def get_latest_rendered(self) -> Optional[Frame]:
        """获取最新渲染帧（由 WebSocket 前端推流调用）。"""
        with self._viz_lock:
            return self._latest_rendered

    # --- latest_inference 操作（原子推理快照）---

    def set_latest_inference(self, result: "FrameInference") -> None:
        """原子写入最新推理结果（由 InferenceLoop 调用）。

        写门：非 ACTIVE 拒——迟到推理结果落到旧 CQ 被拒，不串台。
        """
        if self._state is not RunState.ACTIVE:
            return
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

    def take_raw_segment(self) -> Optional[List[Frame]]:
        """缓冲攒满一整段(ca_segment_len 帧)则原子弹出，否则 None。

        供 persistence 的 HLSSegmentSweeper 周期拉取。与 append_ca_raw / drain_ca_raw /
        _release_payload 同走 _raw_lock，互斥安全。
        """
        with self._raw_lock:
            if len(self.ca_raw) < self.ca_segment_len:
                return None
            return [self.ca_raw.popleft() for _ in range(self.ca_segment_len)]

    def take_processed_segment(self) -> Optional[List[Frame]]:
        """缓冲攒满一整段则原子弹出，否则 None（供 HLSSegmentSweeper 周期拉取）。"""
        with self._viz_lock:
            if len(self.ca_processed) < self.ca_segment_len:
                return None
            return [self.ca_processed.popleft() for _ in range(self.ca_segment_len)]

    # --- 运行状态机 ---

    def get_state(self) -> RunState:
        """当前运行状态（免锁：枚举原子读 + 单调推进）。"""
        return self._state

    def is_active(self) -> bool:
        return self._state is RunState.ACTIVE

    def to_draining(self) -> bool:
        """ACTIVE→DRAINING（幂等、单调）。返回本次是否发生转换。

        拆除入口调用：封生产者写，仍放行 settlement 告警 + HLS flush。
        """
        with self._state_lock:
            if self._state is RunState.ACTIVE:
                self._state = RunState.DRAINING
                return True
            return False

    def close(self) -> None:
        """置 CLOSED（幂等）并释放重数据 payload，保留不可变身份小壳。

        CLOSED 后一切写被拒、读返空；身份（task/step/stage/source_ip）仍可读供 fence/日志。
        """
        with self._state_lock:
            self._state = RunState.CLOSED
        self._release_payload()

    def clear(self) -> None:
        """兼容入口：等价 `close()`（供 ClientManager.remove/remove_if/clear_all 调用）。"""
        self.close()

    def _release_payload(self) -> None:
        """原子释放所有队列和重数据缓存（按全清顺序持 7 把 payload 锁）。

        身份（task/task_id/step_id/source_ip/stage）不可变，不在此重置——仅释放
        payload（帧/滑窗/告警/快照）回收内存（尤其大块 numpy 帧）。
        """
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
            self._latest_rendered = None
            self._latest_inference = None
            self._latest_temporal = []
            self._slide_window.clear()
            self._alarm_log.clear()
            self._alarm_seq = 0
            self._alarm_gate.clear()

    def pop_ca_ready(self) -> Optional[Frame]:
        """从推理队列弹出一帧（FIFO，无锁 SPSC）"""
        return self.ca_ready.popleft() if self.ca_ready else None

    # 身份字段（task_id/step_id/source_ip/stage）均为构造定死的不可变
    # 公有属性，直读即可——不再提供 get_* 包装（避免只覆盖一部分、直连/getter 混用的不一致）。

    # --- slide_window 操作 ---

    def set_stream_windows(self, windows: Dict[str, float]) -> None:
        """配置 per-stream 感受野（{流名: 最大 window_seconds}），整体替换。

        由 InferenceManager 在算子实例化后调用：缓冲保留时长取
        max(底线 10s, 该流感受野)，故感受野只向上扩展，signals_10s 的 10s 不受影响。
        """
        with self._slide_window_lock:
            self._stream_windows = dict(windows)

    def push_detection(self, task_name: str, output: FrameDetections) -> None:
        """将 FrameDetections 追加到 per-stream 滑动窗口，按感受野自动淘汰过期条目。

        写门：非 ACTIVE 拒——迟到检测写回落到旧 CQ 被拒。
        """
        if self._state is not RunState.ACTIVE:
            return
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
        """覆写最新时序事件列表（由 TemporalWorker 调用）。

        写门：非 ACTIVE 拒**非空**写；清空([])放行——拆除期允许清掉前端残留事件。
        """
        if events and self._state is not RunState.ACTIVE:
            return
        with self._frontend_lock:
            self._latest_temporal = events

    def get_latest_temporal(self) -> List[str]:
        """读取最新时序事件列表。"""
        with self._frontend_lock:
            return list(self._latest_temporal)

    # --- alarm_log 操作 ---

    def append_alarm_record_with_gate(self, alarm: Alarm, mode: str) -> bool:
        """闸门去重 + 入环形日志，单 _alarm_lock 内原子完成。

        True = 已记录（赋 seq 并入日志），False = 被冷却窗口（5s）拦截、未记录。
        闸门按 (self.task_id, alarm.metric, mode) 限流；通过后才赋 seq、append。
        task_id 取自本 CQ 不可变身份（免锁直读），无需调用方传入。

        写门（非对称）：仅 CLOSED 拒——ACTIVE 与 DRAINING 均放行，保证拆除期（DRAINING）
        的 settlement 结算告警仍能入账。
        """
        if self._state is RunState.CLOSED:
            return False
        gate_key = f"{self.task_id}:{alarm.metric}:{mode}"
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

    def get_task_alarm_message(self, since_seq: int = 0) -> Dict[str, Any]:
        # task_id 取自本 CQ 不可变身份（注册表以 task_id 为键取到本 CQ，恒相等）。
        # alarm 列表与 max_seq 在同一把锁内读取，保证 max_seq >= max(a.seq for a in alarms)，
        # 避免客户端用推进后的 since_seq 跳过尚未返回的告警。
        # signals_10s 独立持 _slide_window_lock，两路数据本就独立，轻微时差可接受。
        with self._alarm_lock:
            alarms = [a for a in self._alarm_log if a.seq > since_seq]
            max_seq = self._alarm_seq
        return {
            "task_id": self.task_id,
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
        stage = self.stage
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
            "client_id": self.source_ip,  # wire 兼容：该字段历史含义即 source_ip
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
