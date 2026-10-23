"""
客户端队列管理
"""

from __future__ import annotations

import contextlib
import enum
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Set, Tuple

from app.types.alarm import Alarm
from app.types.detection import FrameDetection
from app.types.frame import Frame
from app.types.run import RunIdentity
from app.services.session.core import Session, SessionInfo
from app.services.session.topics import RAW, READY
from app.services.utils.metrics import frame_drop_total
from app.services.utils.pressure import (
    DEFAULT_HIGH_WATERMARK_RATIO,
    REASON_QUEUE_HIGH_WATERMARK,
    PressureReporter,
)
from app.services.utils.startup_latency import log_milestone


class RunState(enum.Enum):
    """一次 run（一个 CQ）的生命周期状态，单调推进 ACTIVE→DRAINING→CLOSED。

    - ACTIVE：正常运行，所有写放行。
    - DRAINING：拆除中，停生产者写（decoder/结果写回/tick），仅放行 settlement 告警 + HLS flush。
    - CLOSED：payload 已释放，一切写被拒；身份小壳仍可读（供 fence/日志）。

    门控在写入**时刻**判 state（非 dispatch 时刻）：迟到写落到 DRAINING/CLOSED 的旧 CQ 被拒，
    不串台到新 run。状态读免锁（枚举原子读 + 单调）；`_state_lock` 仅串行/幂等转换本身。
    """

    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"


class ClientQueues:
    """
    架构说明：
    - CA-ProcessedQueue: 推理后的处理帧（含标注），用于生成处理后 HLS 段（设置最大长度防止溢出）
    - CA-DetectionQueue: 帧级 FrameDetection 落盘缓冲，等 recording 周期拉走写 detections.jsonl
    - _latest_rendered: 单槽位，最新渲染帧，供前端 WebSocket 实时推流
    （待推理帧 / 原始帧 / 最新原始帧、推理快照、时序叠字、帧窗已迁成 topic，见 session/topics.py、
    inference/online/topics.py）

    内存保护：
    - 所有队列都设置了 maxlen 限制，当队列满时自动丢弃最旧的帧
    - ca_processed 默认 900 帧（生产值由 settings.ca_maxlen_seconds × raw_fps 派生，此默认仅裸建/测试兜底）

    锁清单（Lock Inventory）：
      _viz_lock         Lock   ca_processed + _latest_rendered（VizWorker 对同帧连续写两者）
      _slide_window_lock Lock  ca_detections
      _alarm_lock       Lock   _alarm_log + _alarm_seq + _alarm_gate（告警生命周期）
      *_pressure 内建锁  Lock   叶子锁（PressureReporter 自持，只护其几个标量）：
                               append_* 一律**先出队列锁再上报**，故不与上面任何锁互嵌

    全清顺序（clear() 同时持锁时的固定顺序，防死锁）：
      _viz_lock → _slide_window_lock → _alarm_lock

    身份（run/source_ip/stage）为构造定死的不可变值，热路径免锁直读。
    """

    def __init__(
        self,
        ca_segment_len: int = 150,
        ca_maxlen: int = 900,
        resize_width: int = 640,
        resize_height: int = 480,
        *,
        # 不可变运行身份（一次 CQ == 一次 run，终生不变）。
        # 全默认 None/"" 供纯队列/算子单测裸建；生产由 RunControlService 传入 `runs.allocate`
        # 分配的 RunIdentity 与已解析好的 stage。
        run: Optional[RunIdentity] = None,
        source_ip: str = "",
        stage: str = "",
        sess: Optional[Session] = None,
    ):
        # 尺寸配置
        self.resize_width = resize_width
        self.resize_height = resize_height

        # --- 锁声明（顺序同 Lock Inventory 全清顺序）---
        self._viz_lock = threading.Lock()        # ca_processed + _latest_rendered
        self._slide_window_lock = threading.Lock()
        self._alarm_lock = threading.Lock()      # _alarm_log + _alarm_seq + _alarm_gate

        # 运行状态机的锁（只串行/幂等本状态转换）
        self._state: RunState = RunState.ACTIVE
        self._state_lock = threading.Lock()

        # 不可变运行身份：一次构造定死，直读、无锁——CQ 经 client_service COW
        # 换引用发布，读者原子读引用即 acquire，观察不到半建对象。切 step/重启 = 建新 CQ 换槽，
        # 不在此对象上改身份。故 settlement 归属天然正确，无需"先停旧 actor 再切字段"的排序不变式。
        # 注：无 client_id 字段——注册表路由键即 run.task_id(int)；source_ip 为被动来源字段。
        # 「未绑定 run」只有一个判据：`self.run is None`。
        self.run: Optional[RunIdentity] = run
        self.source_ip: str = source_ip
        self.stage: str = stage
        # run 起始时刻：供 HealthMonitorWorker 的 task_max_duration 看门狗判定跑飞任务并超时拆除
        # （daemons/health_monitor/worker.py 用 now - task_started_at ≥ task_max_duration 触发 _handle_task_timeout）。
        # 同时作为启动延迟埋点的公共参考钟（mark_startup_milestone 相对它计耗时）。
        self.task_started_at: float = (
            sess.info.started_at if sess is not None
            else time.time() if run is not None else 0.0
        )

        # 迁移桥：已迁成 topic 的数据经 `cq.sess.channel(T)` 读写。生产由 RunControlService 传入
        # 已注册的 session；裸建（测试）现造一个不入注册表的。状态随 to_draining / close 同步。
        self.sess: Session = sess if sess is not None else Session(SessionInfo(
            run=run, source_ip=source_ip, stage=stage, started_at=self.task_started_at,
        ))

        # 启动里程碑埋点（纯观测，first-wins 幂等）：记录每个里程碑首次到达相对
        # task_started_at 的耗时，仅记一次。无锁——每个 milestone 名字单一生产者线程
        # （first_inference=写回 / first_rendered=viz），同名无并发写。纯日志，不参与任何数据流。
        # first_frame 由 decoder 自行判断首次（见 stream/decoder.py）。
        self._startup_marks: Set[str] = set()

        self.ca_maxlen = ca_maxlen
        # CA-ProcessedQueue（由 _viz_lock 保护）
        self.ca_processed: Deque[Frame] = deque(maxlen=ca_maxlen)
        self.frames_dropped_processed: int = 0
        self.ca_segment_len = ca_segment_len

        # 压力日志（[PRESSURE]）：processed 由 sweeper 整段拉走，深度在 0~1 段之间来回是常态：
        # 水位至少 1.5 段（sweeper 落后半段才算压力），否则段长 = 半缓存时每段都误报。不超过 ca_maxlen。
        self._pressure_watermark: int = max(1, min(
            ca_maxlen,
            max(int(ca_maxlen * DEFAULT_HIGH_WATERMARK_RATIO), ca_segment_len * 3 // 2),
        ))
        self._processed_pressure = PressureReporter(
            "client_queues", "ca_processed",
            identity={
                "task_id": run.task_id if run else None,
                "step_id": run.step_id if run else None,
                "stage": stage,
            },
        )

        # 最新渲染帧（单槽位，由 _viz_lock 保护，供前端 WebSocket 实时推流）
        self._latest_rendered: Optional[Frame] = None

        # CA-DetectionQueue：帧级 FrameDetection 落盘缓冲（由 _slide_window_lock 保护）。
        # 与帧窗 DETECTION_WINDOW 是同一份数据的两个去处——前者供算子消费（按感受野裁剪、会丢），
        # 后者等 recording 每 tick 拉走落盘（只在满时丢最旧）。**不能合并**：裁剪口径归算子
        # 配置，拿它当落盘缓冲会随某个 stage 调小 window_seconds 而静默丢检测结果。
        # 容量复用 ca_maxlen（900 条 ≈ 60s @15fps ≈ 2MB，远小于 ca_raw 的像素帧）。
        self.ca_detections: Deque[FrameDetection] = deque(maxlen=ca_maxlen)
        self.frames_dropped_detections: int = 0

        # 告警日志（由 _alarm_lock 保护）
        self._alarm_log: Deque[Alarm] = deque(maxlen=100)
        self._alarm_seq: int = 0

        # 告警 gate（由 _alarm_lock 保护，与 _alarm_log 合并）
        self._alarm_gate: Dict[str, float] = {}
        self._alarm_gate_window: float = 5.0

    # --- 封装操作方法 ---

    def append_ca_processed(self, frame_data: Frame) -> None:
        """
        添加处理帧到落盘缓冲（纯缓冲，不触发落盘）。

        分段落盘由 recording 的 SegmentSweeper 周期 take_processed_segment() 拉取，
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
            # 锁内只取标量快照（队头 ts 仅在越水位时多读一次，平稳期不付代价）
            depth = len(self.ca_processed)
            drops = self.frames_dropped_processed
            oldest_ts = (
                self.ca_processed[0].timestamp if depth >= self._pressure_watermark else None
            )
        # 锁外上报：谓词只写水位，"累计丢帧仍在涨"由 reporter 并入判定（见 pressure.py）
        self._processed_pressure.observe(
            depth >= self._pressure_watermark,
            reason=REASON_QUEUE_HIGH_WATERMARK,
            depth=depth,
            capacity=self.ca_maxlen,
            utilization=(depth / self.ca_maxlen) if self.ca_maxlen else None,
            oldest_age_ms=((time.time() - oldest_ts) * 1000.0) if oldest_ts is not None else None,
            drop_total=drops,
        )

    def set_latest_rendered(self, frame_data: Optional[Frame]) -> None:
        """更新最新渲染帧（由 VisualizationWorker 调用）。传 None 表示清空。

        写门：非 ACTIVE 拒**非空**写；清空(None)放行——拆除期允许清掉前端残帧。
        """
        if frame_data is not None and self._state is not RunState.ACTIVE:
            return
        with self._viz_lock:
            self._latest_rendered = frame_data
        # 启动延迟埋点 C：首个渲染帧就绪（即首个可供 WS 推送给前端的画面；
        # 仅非空写触发，清空(None)不计。单生产者=唯一 viz worker 线程，幂等）
        if frame_data is not None:
            self.mark_startup_milestone("first_rendered")

    def get_latest_rendered(self) -> Optional[Frame]:
        """获取最新渲染帧（由 WebSocket 前端推流调用）。"""
        with self._viz_lock:
            return self._latest_rendered

    def get_queue_depths(self) -> dict:
        return {
            "ca_ready": len(self.sess.channel(READY)),
            "ca_raw": len(self.sess.channel(RAW)),
            "ca_processed": len(self.ca_processed),
            "ca_detections": len(self.ca_detections),
            "has_rendered": self._latest_rendered is not None,
        }

    def drain_ca_processed(self, until_ts: Optional[float] = None) -> List[Frame]:
        """原子排空 ca_processed 队列（线程安全，供 flush 使用）。

        Args:
            until_ts: 时间戳栅栏（epoch 秒）。`None` = 全排空（拆除期语义，队列不会再进新帧）；
                给值则只弹出队首那段 `timestamp <= until_ts` 的连续前缀，其余留在队列里。
        """
        with self._viz_lock:
            if until_ts is None:
                frames = list(self.ca_processed)
                self.ca_processed.clear()
                return frames
            taken: List[Frame] = []
            while self.ca_processed and self.ca_processed[0].timestamp <= until_ts:
                taken.append(self.ca_processed.popleft())
            return taken

    def take_processed_segment(self) -> Optional[List[Frame]]:
        """缓冲攒满一整段则原子弹出，否则 None（供 recording 的 SegmentSweeper 周期拉取）。"""
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

    # --- 启动延迟埋点 ---

    def mark_startup_milestone(self, name: str) -> None:
        """记录某启动里程碑首次到达（相对 run 起始 task_started_at 的耗时），仅首次记录、幂等。

        name ∈ {"first_inference"(首个推理写回) / "first_rendered"(首个渲染帧就绪)}；
        first_frame 由 decoder 自行判断首次。task_started_at 未置（裸建单测）则跳过。
        """
        if name in self._startup_marks:
            return
        if self.task_started_at <= 0.0:
            return
        self._startup_marks.add(name)
        log_milestone(
            name, task_id=self.run.task_id, step_id=self.run.step_id, started_at=self.task_started_at,
        )

    def to_draining(self) -> bool:
        """ACTIVE→DRAINING（幂等、单调）。返回本次是否发生转换。

        拆除入口调用：封生产者写，仍放行 settlement 告警 + HLS flush。
        """
        with self._state_lock:
            if self._state is RunState.ACTIVE:
                self._state = RunState.DRAINING
                changed = True
            else:
                changed = False
        if changed:
            self._processed_pressure.reset()
        self.sess.seal()
        return changed

    def close(self) -> None:
        """置 CLOSED（幂等）并释放重数据 payload，保留不可变身份小壳。

        CLOSED 后一切写被拒、读返空；身份（task/step/stage/source_ip）仍可读供 fence/日志。
        """
        with self._state_lock:
            self._state = RunState.CLOSED
        self._processed_pressure.reset()
        self._release_payload()
        self.sess.close()

    def clear(self) -> None:
        """兼容入口：等价 `close()`（供 ClientService.remove/remove_if/clear_all 调用）。"""
        self.close()

    def _release_payload(self) -> None:
        """原子释放所有队列和重数据缓存（按全清顺序持 payload 锁）。

        身份（task/task_id/step_id/source_ip/stage）不可变，不在此重置——仅释放
        payload（帧/告警/快照）回收内存（尤其大块 numpy 帧）。
        """
        locks = [self._viz_lock, self._slide_window_lock, self._alarm_lock]
        with contextlib.ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock)
            self.ca_processed.clear()
            self._latest_rendered = None
            self.ca_detections.clear()
            self._alarm_log.clear()
            self._alarm_seq = 0
            self._alarm_gate.clear()

    # 身份字段（task_id/step_id/source_ip/stage）均为构造定死的不可变
    # 公有属性，直读即可——不再提供 get_* 包装（避免只覆盖一部分、直连/getter 混用的不一致）。

    # --- ca_detections 操作（落盘缓冲）---

    def append_ca_detections(self, frame: FrameDetection) -> None:
        """把一帧 FrameDetection 放进落盘缓冲（纯缓冲，不触发落盘）。

        落盘由 recording 的 sweeper 周期 `drain_ca_detections()` 拉走，本方法只管入队 + 丢帧计数。
        写门：非 ACTIVE 拒写——迟到写回落到旧 CQ 被拒，不串台。

        与帧窗 DETECTION_WINDOW 是写回口对同一帧的两次投递（消费 / 落盘）。
        """
        if self._state is not RunState.ACTIVE:
            return
        with self._slide_window_lock:
            if (
                self.ca_detections.maxlen is not None
                and len(self.ca_detections) >= self.ca_detections.maxlen
            ):
                # deque 满时 append 静默淘汰最旧，先计数
                self.frames_dropped_detections += 1
                frame_drop_total.labels(reason="detection_backpressure").inc()
            self.ca_detections.append(frame)

    def drain_ca_detections(self) -> List[FrameDetection]:
        """原子排空落盘缓冲（由 recording 的 sweeper 每 tick 调）。

        **没有 `until_ts` 栅栏**，与帧队列的不对称是有意的：栅栏是给断流用的，
        而它要挡的是"一段视频横跨 gap 被 `effective_fps` 反推成慢放"——detections 每帧一行、
        行间无依赖，断流在序列里就是一个 ts 空洞，离线按 ts 自行处理。
        """
        with self._slide_window_lock:
            frames = list(self.ca_detections)
            self.ca_detections.clear()
            return frames

    # --- alarm_log 操作 ---

    def append_alarm_record_with_gate(self, alarm: Alarm, mode: str) -> bool:
        """闸门去重 + 入环形日志，单 _alarm_lock 内原子完成。

        True = 已记录（赋 seq 并入日志），False = 被冷却窗口（5s）拦截、未记录。
        闸门按 (alarm.metric, mode) 限流（闸门表本就挂在这个 CQ 上，一 CQ 一 run，键里不再带 task）；
        通过后才赋 seq、append。

        写门（非对称）：仅 CLOSED 拒——ACTIVE 与 DRAINING 均放行，保证拆除期（DRAINING）
        的 settlement 结算告警仍能入账。
        """
        if self._state is RunState.CLOSED:
            return False
        gate_key = f"{alarm.metric}:{mode}"
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
