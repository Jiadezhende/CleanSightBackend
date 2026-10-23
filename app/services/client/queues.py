"""
客户端队列管理（迁移桥）：帧与检测数据已全部迁成 session topic，CQ 只剩身份、状态、告警账本与启动埋点。
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Set, Tuple

from app.types.alarm import Alarm
from app.types.run import RunIdentity
from app.services.session.core import Session, SessionInfo
from app.services.session.topics import DETECTIONS, PROCESSED, RAW, READY, RENDERED
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
    一次 run 的 CQ。帧 / 检测 / 快照数据都在 `self.sess` 的 topic 上
    （见 session/topics.py、inference/online/topics.py），本类只剩：

    - 不可变身份（run / source_ip / stage / task_started_at）与运行状态（镜像到 session）
    - 告警账本：_alarm_log + _alarm_seq + _alarm_gate（由 _alarm_lock 保护）
    - 启动埋点 first_inference 的去重集合
    - 录像段长 ca_segment_len

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
        self.ca_maxlen = ca_maxlen
        self.ca_segment_len = ca_segment_len

        self._alarm_lock = threading.Lock()      # L2：_alarm_log + _alarm_seq + _alarm_gate

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
        # run 起始时刻：供 HealthMonitorWorker 的 task_max_duration 看门狗判定跑飞任务并超时拆除。
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

        # 启动里程碑埋点 first_inference（写回线程单一生产者）的 first-wins 去重。纯日志。
        # first_frame / first_rendered 由 decoder / viz 自行判断首次。
        self._startup_marks: Set[str] = set()

        # 告警日志（由 _alarm_lock 保护）
        self._alarm_log: Deque[Alarm] = deque(maxlen=100)
        self._alarm_seq: int = 0

        # 告警 gate（由 _alarm_lock 保护，与 _alarm_log 合并）
        self._alarm_gate: Dict[str, float] = {}
        self._alarm_gate_window: float = 5.0

    def get_queue_depths(self) -> dict:
        """admin / 状态汇总用的深度快照（键名沿用 CQ 时代，对外不变）。"""
        sess = self.sess
        return {
            "ca_ready": len(sess.channel(READY)),
            "ca_raw": len(sess.channel(RAW)),
            "ca_processed": len(sess.channel(PROCESSED)),
            "ca_detections": len(sess.channel(DETECTIONS)),
            "has_rendered": sess.channel(RENDERED).get() is not None,
        }

    # --- 运行状态机 ---

    def get_state(self) -> RunState:
        """当前运行状态（免锁：枚举原子读 + 单调推进）。"""
        return self._state

    def is_active(self) -> bool:
        return self._state is RunState.ACTIVE

    # --- 启动延迟埋点 ---

    def mark_startup_milestone(self, name: str) -> None:
        """记录某启动里程碑首次到达（相对 run 起始 task_started_at 的耗时），仅首次记录、幂等。

        task_started_at 未置（裸建单测）则跳过。
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
        self.sess.seal()
        return changed

    def close(self) -> None:
        """置 CLOSED（幂等）并释放告警账本与 session 数据，保留不可变身份小壳。

        CLOSED 后一切写被拒、读返空；身份（task/step/stage/source_ip）仍可读供 fence/日志。
        """
        with self._state_lock:
            self._state = RunState.CLOSED
        with self._alarm_lock:
            self._alarm_log.clear()
            self._alarm_seq = 0
            self._alarm_gate.clear()
        self.sess.close()

    def clear(self) -> None:
        """兼容入口：等价 `close()`（供 ClientService.remove/remove_if/clear_all 调用）。"""
        self.close()

    # 身份字段（task_id/step_id/source_ip/stage）均为构造定死的不可变
    # 公有属性，直读即可——不再提供 get_* 包装（避免只覆盖一部分、直连/getter 混用的不一致）。

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
