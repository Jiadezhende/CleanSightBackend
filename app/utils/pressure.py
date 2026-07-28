"""压力日志上报公共件（`[PRESSURE]` 单行 key=value 周期快照）。

**只描述，不决策**：本模块只负责把「哪里有压力、积压多少、最近丢/拒多少」写成一条稳定可 grep
的日志行。降帧、限流、重启、告警一律不在此发生。

形态是**周期快照**：到点（`interval`，默认 10s）且当下有压力才打一行，平稳时完全静默。
不做「进入/持续/恢复」的边沿状态机——链路尚未遇到真实压力，先要的是一条能 grep 的心跳，
而不是精确到毫秒的压力窗口起止。代价是**瞬时尖峰可能被采样点错过**，接受。

只有两类资源接它：`ClientQueues` 的三条 CA 队列、`StageAwareDispatcher` 的 stage deque。
其余位置（子进程死亡、落盘失败、读文件失败）是**事件**不是状态，照常直接打日志。

**专用 logger `app.pressure`**（同 `app.startup_latency` 的做法）：所有压力行走同一个名字，
与业务日志解耦——嫌吵可一行关掉全部压力行（`getLogger("app.pressure").setLevel(ERROR)`）、
或单独路由到一个文件，而不影响 queues/dispatcher 自身的日志。调用方因此**不传 logger**。

关键约定：
- **限频**：每 `interval` 至多一条，过载时日志系统不会先被打满。
- **压力 = 调用方谓词 OR 任一 `*_total` 自上次报告后增长**：累计值只说明"历史上发生过"，
  delta 才说明"此刻仍在丢"；而且丢完就空、水位天然测不到，只有 delta 能报出来。
- **绝不影响热路径**：`observe()` 整体 try/except；内建锁只护本对象几个标量，
  从不与调用方的队列锁互嵌（调用方须在自己的锁**外**调用本类）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

__all__ = [
    "PressureReporter",
    "PRESSURE_LOGGER_NAME",
    "DEFAULT_HIGH_WATERMARK_RATIO",
    "DEFAULT_REPORT_INTERVAL",
    "REASON_QUEUE_HIGH_WATERMARK",
]

# 压力日志专用 logger：全链路压力行的唯一出口（独立于各业务模块的 logger）。
PRESSURE_LOGGER_NAME = "app.pressure"
_logger = logging.getLogger(PRESSURE_LOGGER_NAME)

# 稳定 reason 常量（低基数、可 grep；异常文本一律不作 reason）。
# 目前只有「队列积压」一种——成因由行内字段区分：`reject_delta>0` 即下游在拒收，
# 否则就是取帧快于提交。别为每种成因再造一个 reason。
REASON_QUEUE_HIGH_WATERMARK = "queue_high_watermark"

# depth/capacity ≥ 此值即视为积压前兆（不是满才叫压力——满时已经在丢了）
DEFAULT_HIGH_WATERMARK_RATIO = 0.5
# 周期上报间隔（秒）
DEFAULT_REPORT_INTERVAL = 10.0

# 以此后缀结尾的字段被视为累计计数：reporter 自动补一个同名 `*_delta`
# （= 自上次**实际打印**以来的增量），并据其是否 >0 参与压力判定。
_TOTAL_SUFFIX = "_total"


class PressureReporter:
    """单个资源的周期压力快照上报器。

    一个实例对应一个 (component, resource[, identity]) 三元组，例如
    `(client_queues, ca_processed, task_id=7)` 或 `(dispatcher, stage_queue, stage=CLEAN)`。
    每个资源只应有一个所有者持有其 reporter。
    """

    def __init__(
        self,
        component: str,
        resource: str,
        *,
        interval: float = DEFAULT_REPORT_INTERVAL,
        identity: Optional[Dict[str, Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Args:
            component: 组件名（client_queues / dispatcher）；日志固定走 `app.pressure`，不传 logger。
            resource: 被观察资源（ca_processed / stage_queue …）。
            interval: 上报间隔（秒）；两条之间无论被 observe 多少次都只打一条。
            identity: 每行固定携带的身份字段（task_id / step_id / stage …），值为 None 的不打。
            clock: 单调钟（不受系统时间调整影响），测试注入假钟。
        """
        self.component = component
        self.resource = resource
        self._interval = interval
        self._identity = {k: v for k, v in (identity or {}).items() if v is not None}
        self._clock = clock

        # 只护本对象下面几个标量；绝不在持有调用方队列锁时被 acquire（调用方须锁外调用）
        self._lock = threading.Lock()
        self._last_report_at = 0.0
        # 上次**打印**时各 `*_total` 的值（delta 基线）。仅打印后推进，
        # 使 delta 恒为「自上次报告以来」而非「自上次 observe 以来」。
        self._baselines: Dict[str, float] = {}

    def observe(
        self,
        pressured: bool,
        *,
        reason: Optional[str] = None,
        **fields: Any,
    ) -> None:
        """喂一次快照；到点且有压力就打一条 `[PRESSURE]`，否则静默。

        Args:
            pressured: 调用方谓词（水位越线等）。实际判定为 `pressured or 任一 *_total 增长`。
            reason: 稳定 reason 常量。
            **fields: 观测字段。名字以 `_total` 结尾的自动补 `*_delta`；值为 None 的不打印
                （不适用的字段直接不打，不用 -1 等魔法值）。

        绝不抛出：日志失败不得影响入队/提交/写回。
        """
        try:
            self._observe(pressured, reason, fields)
        except Exception:  # pragma: no cover - 兜底：日志永不炸热路径
            pass

    def reset(self) -> None:
        """清空计时与 delta 基线（run 拆除 / 组件换代时用），不打日志。"""
        with self._lock:
            self._last_report_at = 0.0
            self._baselines.clear()

    # ────────────────────────── 内部 ──────────────────────────

    def _observe(self, pressured: bool, reason: Optional[str], fields: Dict[str, Any]) -> None:
        now = self._clock()

        with self._lock:
            # 1. 累计计数 → delta（相对上次打印的基线）。首次见到某计数时就地播种（delta=0），
            #    避免把进程启动前的历史累计当成"刚刚丢的"。
            deltas: Dict[str, float] = {}
            growing = False
            for name, value in fields.items():
                if not name.endswith(_TOTAL_SUFFIX) or value is None:
                    continue
                if name not in self._baselines:
                    self._baselines[name] = value
                delta = value - self._baselines[name]
                deltas[name[: -len(_TOTAL_SUFFIX)] + "_delta"] = delta
                if delta > 0:
                    growing = True

            # 2. 平稳则静默；「还在丢」也算有压力（丢完就空，水位测不到）
            if not (pressured or growing):
                return
            # 3. 限频：两条之间无论被 observe 多少次都只打一条
            if now - self._last_report_at < self._interval:
                return

            # 4. 打印后才推进基线，使 delta = 自上次报告以来的增量
            for name, value in fields.items():
                if name.endswith(_TOTAL_SUFFIX) and value is not None:
                    self._baselines[name] = value
            self._last_report_at = now

            line = self._format(reason, fields, deltas)

        # 锁外写日志（logging 自身可能持锁/落盘，不该压在本对象的锁里）
        _logger.warning("%s", line)

    def _format(
        self,
        reason: Optional[str],
        fields: Dict[str, Any],
        deltas: Dict[str, float],
    ) -> str:
        """拼单行 key=value。**只在确定要打印时才调用**（平稳期零字符串开销）。"""
        parts = ["[PRESSURE]", f"component={self.component}", f"resource={self.resource}"]
        for key, value in self._identity.items():
            parts.append(f"{key}={value}")
        for key, value in fields.items():
            if value is None:
                continue
            parts.append(f"{key}={_fmt(value)}")
            if key.endswith(_TOTAL_SUFFIX):
                delta_key = key[: -len(_TOTAL_SUFFIX)] + "_delta"
                if delta_key in deltas:
                    parts.append(f"{delta_key}={_fmt(deltas[delta_key])}")
        if reason:
            parts.append(f"reason={reason}")
        return " ".join(parts)


def _fmt(value: Any) -> str:
    """数值定宽格式化：整数原样、浮点 3 位（utilization 0.703 / age 4000 皆可读）。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 1000 else f"{value:.0f}"
    return str(value)
