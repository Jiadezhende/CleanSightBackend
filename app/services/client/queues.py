"""
客户端队列管理
"""

from __future__ import annotations

import contextlib
import enum
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from app.domain.alarm import Alarm
from app.domain.detection import FrameFeature
from app.domain.frame import Frame
from app.utils.metrics import frame_drop_total

# signals_10s 聚合底线（固定 10s），与帧窗保留时长（可 ≥10s）解耦。
_SIGNALS_WINDOW_SEC = 10.0


class RunState(enum.Enum):
    """一次 run（一个 CQ）的生命周期状态，单调推进 ACTIVE→DRAINING→CLOSED。

    - ACTIVE：正常运行，所有写放行。
    - DRAINING：拆除中，停生产者写（decoder/结果写回/tick），仅放行 settlement 告警 + HLS flush。
    - CLOSED：payload 已释放，一切写被拒；身份小壳仍可读（供 fence/日志）。

    门控在写入**时刻**判 state（非 dispatch 时刻）：迟到写落到 DRAINING/CLOSED 的旧 CQ 被拒，
    不串台到新 run。状态读免锁（枚举原子读 + 单调）；`_state_lock` 仅串行/幂等转换本身，
    绝不与 6 把 payload 锁互嵌——无新锁环。
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
      ca_ready          无锁   SPSC deque：单生产者 decoder / 单消费者 dispatcher，GIL 保证原子性
      _raw_lock         Lock   ca_raw + latest_raw_frame + latest_raw_timestamp
      _viz_lock         Lock   ca_processed + _latest_rendered（VizWorker 对同帧连续写两者）
      _inference_lock   Lock   _latest_inference（原子推理快照槽）
      _frontend_lock    Lock   _latest_temporal（前端时序事件，低频写）
      _slide_window_lock Lock  _slide_window dict（推理每帧写入，最高竞争锁）
      _alarm_lock       Lock   _alarm_log + _alarm_seq + _alarm_gate（告警生命周期）

    全清顺序（clear() 同时持锁时的固定顺序，防死锁）：
      _raw_lock → _viz_lock → _inference_lock
      → _frontend_lock → _slide_window_lock → _alarm_lock

    身份（task_id/step_id/source_ip/stage）为构造定死的不可变 primitive，热路径免锁直读。
    """

    def __init__(
        self,
        ca_segment_len: int = 150,
        ca_maxlen: int = 2700,
        resize_width: int = 640,
        resize_height: int = 480,
        inference_decimation: int = 2,
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

        # 抽帧降采样倍率（每 N 帧留 1；N=raw_fps/检测率，如 30fps→15fps 取 N=2）。
        # 抽帧器只需此整数倍率，不引用 raw_fps——源速率概念不进 CQ。
        self.inference_decimation = inference_decimation
        # 抽帧计数器：仅由 decoder 线程读写（append_ca_ready_with_throttle 内部），无并发，不加锁
        self._decimate_counter: int = 0

        # --- 锁声明（顺序同 Lock Inventory 全清顺序）---
        self._raw_lock = threading.Lock()        # ca_raw + 帧缓存
        self._viz_lock = threading.Lock()        # ca_processed + _latest_rendered
        self._inference_lock = threading.Lock()  # _latest_inference
        self._frontend_lock = threading.Lock()   # _latest_temporal
        self._slide_window_lock = threading.Lock()
        self._alarm_lock = threading.Lock()      # _alarm_log + _alarm_seq + _alarm_gate

        # 运行状态机的锁（独立于下方 6 把 payload 锁：只串行/幂等本状态转换，
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

        # 三条 CA 队列共用同一 maxlen（统一容量，非 per-queue）；公有属性直读，
        # 不提供 get_*_capacity 包装（同 ca_segment_len 风格，见类 docstring 身份直读约定）。
        self.ca_maxlen = ca_maxlen
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

        # 最新推理快照：帧级 FrameFeature（由 _inference_lock 保护，供 Viz 原子读同帧一致）
        self._latest_inference: Optional[FrameFeature] = None

        # 滑动窗口：帧级 FrameFeature 环形缓冲（一帧一条，多流已对齐，由 _slide_window_lock 保护）。
        # 写回口物化 FrameFeature 后单次 push_detection；算子/ signals 统一从此读，无需按 ts 拼帧。
        self._slide_window: Deque[FrameFeature] = deque()
        # 帧窗保留时长（秒）：= max(_SIGNALS_WINDOW_SEC 底线, 各算子最大感受野)，由 set_stream_windows 配置。
        # 只向上扩展；signals_10s 聚合另按固定 10s 底线裁窗，二者解耦。
        self._slide_window_seconds: float = _SIGNALS_WINDOW_SEC

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
        添加帧到待推理队列（整数倍率均匀抽帧 + 背压）。

        输入为 ffmpeg 规范化后的 CFR 流：CFR 已把时间烙成等距帧号，故按**帧计数**「每 N 帧
        留 1」即精确均匀降采样（N=inference_decimation），不依赖 wall-clock —— 消除解码线程
        调度抖动导致的真实率漂移（旧墙钟门把 15 漏成 ~12）。整数计数天然精确、无浮点累积误差。
        （整数因子只命中 raw_fps 的整除率；非整除比走不了，模型侧另按 ts 重采样到契约帧率。）

        ca_ready 为无锁 SPSC deque，decoder 是唯一写入方，dispatcher 是唯一消费方。
        _decimate_counter 仅由本方法（decoder 线程）读写，无并发，不加锁。
        """
        # 写门：非 ACTIVE（拆除中/已关）拒写——在推进计数器之前拒，避免 late 帧扰动抽帧节奏。
        if self._state is not RunState.ACTIVE:
            return False

        # 1. 整数倍率选帧：计数器每输入帧 +1，攒满 N 帧放行 1 帧（保留率精确 = 1/N）。
        self._decimate_counter += 1
        if self._decimate_counter < self.inference_decimation:
            return False
        self._decimate_counter = 0

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

    def set_latest_inference(self, result: FrameFeature) -> None:
        """原子写入最新推理快照 FrameFeature（由 InferenceLoop 调用）。

        写门：非 ACTIVE 拒——迟到推理结果落到旧 CQ 被拒，不串台。
        """
        if self._state is not RunState.ACTIVE:
            return
        with self._inference_lock:
            self._latest_inference = result

    def get_latest_inference(self) -> Optional[FrameFeature]:
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
        """原子释放所有队列和重数据缓存（按全清顺序持 6 把 payload 锁）。

        身份（task/task_id/step_id/source_ip/stage）不可变，不在此重置——仅释放
        payload（帧/滑窗/告警/快照）回收内存（尤其大块 numpy 帧）。
        """
        locks = [
            self._raw_lock, self._viz_lock,
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
        """配置帧窗保留时长 = max(10s 底线, 各算子最大感受野)。

        由 InferenceManager 在算子实例化后调用（入参 {流名: 最大 window_seconds}）：
        单条帧窗保留所有算子里最长的感受野，各算子自行 _clip 到自身 window_seconds；
        感受野只向上扩展，signals_10s 另按固定 10s 底线裁窗，不受影响。
        """
        with self._slide_window_lock:
            self._slide_window_seconds = max(
                [_SIGNALS_WINDOW_SEC] + list(windows.values())
            )

    def push_detection(self, feature: FrameFeature) -> None:
        """将一帧对齐后的 FrameFeature 追加到帧窗，按保留时长淘汰过期条目。

        写回口一帧一次（多流已在 FrameFeature.by_source 内对齐），不再逐 detector。
        写门：非 ACTIVE 拒——迟到写回落到旧 CQ 被拒。
        """
        if self._state is not RunState.ACTIVE:
            return
        with self._slide_window_lock:
            self._slide_window.append(feature)
            cutoff = feature.ts - self._slide_window_seconds
            while self._slide_window and self._slide_window[0].ts < cutoff:
                self._slide_window.popleft()

    def get_slide_window(self) -> List[FrameFeature]:
        """返回帧窗的快照副本（线程安全）；每条 = 一帧多流对齐检测。"""
        with self._slide_window_lock:
            return list(self._slide_window)

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

    def get_alarm_snapshot(self, since_seq: int = 0) -> Tuple[List[Alarm], int]:
        """单 _alarm_lock 内原子返回 (seq>since_seq 的告警增量, max_seq)。

        原子读保证 max_seq >= max(a.seq for a in alarms)，避免调用方用推进后的
        since_seq 跳过尚未返回的告警。域对象 Alarm 交由调用方（router）序列化。
        """
        with self._alarm_lock:
            alarms = [a for a in self._alarm_log if a.seq > since_seq]
            return alarms, self._alarm_seq

    def get_slide_window_summary(self) -> Dict[str, Dict[str, Any]]:
        """每条流(stream_name)在其窗口内的检测汇总（线程安全，纯数据）。

        {stream_name: {"active": bool, "hit_count": int, "max_conf": float}}
        —— 只按流名聚合，不做流名→metric 映射（那是 inference 展示知识，归 router 装配）。
        """
        acc: Dict[str, Dict[str, float]] = {}
        with self._slide_window_lock:
            if not self._slide_window:
                return {}
            # signals_10s 固定按 10s 底线裁窗（帧窗可保留更长感受野，此处不受影响）。
            cutoff = self._slide_window[-1].ts - _SIGNALS_WINDOW_SEC
            for feat in self._slide_window:
                if feat.ts < cutoff:
                    continue
                for src, fd in feat.by_source.items():
                    if fd is None:
                        continue
                    a = acc.setdefault(src, {"hit": 0.0, "max_conf": 0.0})
                    if fd.detections:
                        a["hit"] += 1
                        frame_max_conf = max(d.confidence for d in fd.detections)
                        a["max_conf"] = max(a["max_conf"], frame_max_conf)
        return {
            src: {
                "active": a["hit"] > 0,
                "hit_count": int(a["hit"]),
                "max_conf": round(float(a["max_conf"]), 4),
            }
            for src, a in acc.items()
        }
