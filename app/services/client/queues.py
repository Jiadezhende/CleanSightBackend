"""
客户端队列（迁移桥）：数据已全部迁成 session topic，CQ 只剩不可变身份、运行状态与 `sess`。
"""

from __future__ import annotations

import enum
import threading
import time
from typing import Optional

from app.types.run import RunIdentity
from app.services.session.core import Session, SessionInfo
from app.services.session.topics import DETECTIONS, PROCESSED, RAW, READY, RENDERED


class RunState(enum.Enum):
    """一次 run（一个 CQ）的生命周期状态，单调推进 ACTIVE→DRAINING→CLOSED（镜像到 `sess`）。"""

    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"


class ClientQueues:
    """一次 run 的 CQ：不可变身份 + 运行状态 + `sess`（数据都在它的 topic 上）。

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
        self.resize_width = resize_width
        self.resize_height = resize_height
        self.ca_maxlen = ca_maxlen
        self.ca_segment_len = ca_segment_len   # 录像段长（帧），recording 按它整段取走

        # 运行状态机的锁（L2，只串行/幂等本状态转换）
        self._state: RunState = RunState.ACTIVE
        self._state_lock = threading.Lock()

        # 「未绑定 run」只有一个判据：`self.run is None`。
        self.run: Optional[RunIdentity] = run
        self.source_ip: str = source_ip
        self.stage: str = stage
        # run 起始时刻：HealthMonitorWorker 的 task_max_duration 看门狗用它判定跑飞任务。
        self.task_started_at: float = (
            sess.info.started_at if sess is not None
            else time.time() if run is not None else 0.0
        )

        # 迁移桥：数据经 `cq.sess.channel(T)` 读写。生产由 RunControlService 传入
        # 已注册的 session；裸建（测试）现造一个不入注册表的。状态随 to_draining / close 同步。
        self.sess: Session = sess if sess is not None else Session(SessionInfo(
            run=run, source_ip=source_ip, stage=stage, started_at=self.task_started_at,
        ))

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

    def to_draining(self) -> bool:
        """ACTIVE→DRAINING（幂等、单调）。返回本次是否发生转换。"""
        with self._state_lock:
            if self._state is RunState.ACTIVE:
                self._state = RunState.DRAINING
                changed = True
            else:
                changed = False
        self.sess.seal()
        return changed

    def close(self) -> None:
        """置 CLOSED（幂等）并释放 session 数据，保留不可变身份小壳。"""
        with self._state_lock:
            self._state = RunState.CLOSED
        self.sess.close()

    def clear(self) -> None:
        """兼容入口：等价 `close()`（供 ClientService.remove/remove_if/clear_all 调用）。"""
        self.close()
