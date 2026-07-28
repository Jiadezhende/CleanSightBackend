"""worker.py - 可视化工作线程（定时拉取循环）。

职责：
- 按固定间隔（tick_interval）轮询所有活跃客户端
- 从 ClientQueues 主动拉取三要素：
  - cq.get_latest_inference()  → 原子推理快照（所有 task 同帧一致）
  - cq.get_latest_frame()      → 最新原始帧
  - cq.get_latest_temporal()   → 最新时序事件
- 调用 FixedVisualizer（同层 visualizer.py）渲染检测框、标注、文字信息到最新帧上
- 写回 ClientQueues（ca_processed + _latest_rendered）

线程管理在 pool.py（VisualizationWorkerPool）、渲染逻辑在 visualizer.py（FixedVisualizer）。
"""

import logging
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import numpy as np

from app.domain.detection import FrameDetections, FrameFeature
from app.domain.render import RenderSpec
from app.domain.frame import Frame
from app.services.inference.naming import get_stage_alias
from app.services.client import client_manager
from app.services.inference.visualization.visualizer import FixedVisualizer

logger = logging.getLogger(__name__)

# tick 率低于标称轮询率此比例 → 判 viz 线程饥饿（GIL 争用/被抢占，转不满 poll_rate）
_TICK_HEALTH_RATIO = 0.8
# 客户端窗内存活不足此秒数 → 样本太短，只打数不下压力判定（刚起的 run 分母还没铺开）
_MIN_SPAN_SEC = 1.0


class VisualizationWorker:
    """可视化工作线程（定时拉取模式）。

    独立于 TemporalWorker，按自己的节奏（tick_interval）遍历所有活跃客户端，
    从 ClientQueues 拉取原子推理快照 + 最新帧 + 时序事件，渲染后写回。
    """

    def __init__(
        self,
        stop_event: threading.Event,
        tick_interval: float = 1.0 / 20,  # 兜底 ~20 FPS；实际由 pool 按 settings.raw_fps 过采样注入
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        output_fps: Optional[float] = None,
    ):
        """初始化可视化工作线程。

        Args:
            stop_event: 停止事件
            tick_interval: 拉取间隔（秒），由 pool 按 settings.raw_fps 过采样注入（轮询率）
            worker_id: 工作线程ID（用于调试）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
            output_fps: 期望出帧率（= inference_fps），供吞吐告警判「速率亏空」的基准。
                渲染按 inference.ts 去重，出帧上限恒 = 该值，与轮询率（tick）解耦——过采样后
                轮询率 > 出帧率，故不能拿轮询率当基准（否则告警恒常亮）。缺省回退轮询率（兼容
                脱离装配的直接构造/旧测试，此时二者相等）。
        """
        self.stop_event = stop_event
        self.tick_interval = tick_interval
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}
        # 期望出帧率：缺省回退轮询率（1/tick），此时退化为过采样前的旧语义
        self.output_fps = output_fps if output_fps and output_fps > 0 else (
            1.0 / tick_interval if tick_interval > 0 else 0.0
        )
        self.fixed_visualizer = FixedVisualizer()

        # 去重：记录每个 run(键=task_id) 上次渲染的推理时间戳，避免重复渲染同一帧
        self._last_rendered_ts: Dict[int, float] = {}

        # 吞吐量观测（[VIZ_THROUGHPUT]）：每 ~10s 评估一次，仅在有压力时才打印，
        # 平稳时静默以免刷屏。定位三件事：viz 线程本身转没转够（tick 率）、
        # processed 成帧率不足是"上游供帧慢"(supply-bound) 还是"单帧渲染慢"(render-bound)。
        self._win_start: float = 0.0          # 当前统计窗起点（首轮 run 内用 time.time 初始化）
        self._eval_interval: float = 10.0     # 评估窗长（秒）
        self._tick_count: int = 0             # 窗内 _tick() 实际执行次数（viz 线程健康度，与客户端数无关）
        self._first_seen: Dict[int, float] = {}  # 窗内各 run 首次被观测到的时刻（out_fps 的分母起点）
        # 两个统计字典与 _first_seen/_last_rendered_ts 同键（task_id），标注对齐实际键型
        self._stat_rendered: Dict[int, int] = defaultdict(int)  # 实际渲染（=新推理结果）帧数
        self._stat_stale: Dict[int, int] = defaultdict(int)     # 有推理但无新结果而空转的 tick 数
        self._render_time_sum: float = 0.0    # 渲染耗时累计（秒）
        self._render_time_max: float = 0.0    # 单帧渲染耗时峰值（秒）
        self._render_calls: int = 0           # 渲染调用次数

    def run(self):
        """工作循环：固定间隔轮询所有客户端。"""
        logger.debug(
            "[VisualizationWorker-%d] Started (tick=%.3fs, ~%.0f FPS)",
            self.worker_id, self.tick_interval, 1.0 / self.tick_interval,
        )

        while not self.stop_event.is_set():
            tick_start = time.time()
            if self._win_start == 0.0:
                self._win_start = tick_start
            try:
                self._tick(tick_start)
            except Exception as e:
                logger.error(
                    "[VisualizationWorker-%d] Tick exception: %s",
                    self.worker_id, e, exc_info=True,
                )

            # 吞吐量评估：到点（~10s）则算一次，仅有压力时打印
            window = tick_start - self._win_start
            if window >= self._eval_interval:
                self._log_throughput_snapshot(window)
                self._reset_throughput_window(tick_start)

            # 睡眠至下一个 tick
            elapsed = time.time() - tick_start
            sleep_time = max(0, self.tick_interval - elapsed)
            if sleep_time > 0:
                self.stop_event.wait(sleep_time)

        logger.debug("[VisualizationWorker-%d] Stopped", self.worker_id)

    def _tick(self, now: float):
        """一次轮询：遍历所有活跃客户端执行可视化。

        Args:
            now: 本 tick 的起始墙钟（由 run() 传入，供 _first_seen 记录窗内首见时刻；
                同一 tick 内所有客户端共用一个读数，不各自取时钟）。
        """
        # tick 计数在遍历之前：即便本轮无客户端/全抛异常，这一 tick 也算跑过了——
        # 它量的是「viz 线程有没有转够」，与客户端数无关。
        self._tick_count += 1

        all_clients = client_manager.snapshot()
        for task_id, cq in all_clients.items():
            try:
                self._process_client(task_id, cq, now)
            except Exception as e:
                logger.error(
                    "[VisualizationWorker-%d] Error processing run %s: %s",
                    self.worker_id, task_id, e, exc_info=True,
                )

        # 自清理：移除已不在 ClientManager 中的客户端去重记录（防止内存泄漏）
        stale_ids = self._last_rendered_ts.keys() - all_clients.keys()
        for stale_id in stale_ids:
            del self._last_rendered_ts[stale_id]

    def _process_client(self, task_id: int, cq, now: float) -> None:
        """处理单个 run 的可视化。"""
        # 1. 原子读取推理快照（所有 task 同帧一致）
        inference: Optional[FrameFeature] = cq.get_latest_inference()
        if inference is None:
            return

        # 窗内首见时刻：出帧率的分母起点。run 在窗中途才起来时，拿整个窗长当分母会把
        # out_fps 算低（成帧数是真的，只是活得短）——那正是历史上误报 supply-bound 的成因。
        # 记在 None 门之后：有推理快照才算"这条流开始供帧了"。
        if task_id not in self._first_seen:
            self._first_seen[task_id] = now

        # 2. 去重：跳过已渲染过的同一推理结果
        last_ts = self._last_rendered_ts.get(task_id, 0.0)
        if inference.ts <= last_ts:
            # 有推理快照但无新结果 → tick 空转。占比高即"上游供帧慢"的直接信号。
            self._stat_stale[task_id] += 1
            return

        # 3. 获取最新原始帧
        frame = cq.get_latest_frame()
        if frame is None:
            return

        # 4. 获取最新时序事件
        events = cq.get_latest_temporal()

        # 5. 渲染（计时，用于判定是否 render-bound）。stage 取自 cq 不可变身份（快照不再携 stage）。
        stage = cq.stage
        t0 = time.perf_counter()
        annotated_frame = self._render(frame, stage, inference.by_source, events)
        dt = time.perf_counter() - t0
        self._render_time_sum += dt
        self._render_calls += 1
        if dt > self._render_time_max:
            self._render_time_max = dt

        # 6. 写回
        frame_data = Frame(
            timestamp=inference.ts,
            frame=annotated_frame,
        )
        cq.append_ca_processed(frame_data)
        cq.set_latest_rendered(frame_data)

        # 7. 更新去重时间戳 + 计成帧数（实际渲染帧数 = processed 真实成帧率）
        self._last_rendered_ts[task_id] = inference.ts
        self._stat_rendered[task_id] += 1

    def _render(
        self,
        frame: np.ndarray,
        stage: str,
        detection_results: Dict[str, FrameDetections],
        events: List[str],
    ) -> np.ndarray:
        """使用固定渲染器进行可视化。

        Args:
            frame: 原始帧
            stage: 当前阶段
            detection_results: 推理结果 {task_name: FrameDetections}（同帧原子快照）
            events: 时序事件列表
        """
        try:
            # 获取当前 stage 的 tasks（用于调用 prepare_visualization_data）
            stage_cfg = self.stage_configs.get(stage, {})
            tasks = stage_cfg.get("models", [])

            if not tasks:
                return frame.copy()

            vis_data_list: List[RenderSpec] = []

            for task in tasks:
                detection_output = detection_results.get(task.name)

                if not isinstance(detection_output, FrameDetections):
                    continue

                vis_data = task.prepare_visualization_data(detection_output)
                vis_data_list.append(vis_data)

            # 使用固定渲染器渲染。stage 主键是 step_id，叠字需可读别名。
            # 注：render() 内部即 `annotated = frame.copy()`，此处无需再 copy，
            # 否则一帧两次整帧拷贝。render() 全程只改副本、不动入参，传原帧安全。
            annotated_frame = self.fixed_visualizer.render(
                frame=frame,
                vis_data_list=vis_data_list,
                stage=get_stage_alias(stage),
                temporal_events=events,
            )

            return annotated_frame

        except Exception as e:
            logger.error("[VisualizationWorker] Render failed: %s", e, exc_info=True)
            return frame.copy()

    def _reset_throughput_window(self, now: float) -> None:
        """重置吞吐量统计窗。"""
        self._win_start = now
        self._tick_count = 0
        self._first_seen.clear()
        self._stat_rendered.clear()
        self._stat_stale.clear()
        self._render_time_sum = 0.0
        self._render_time_max = 0.0
        self._render_calls = 0

    def _log_throughput_snapshot(self, window: float) -> None:
        """有压力时打一条 [VIZ_THROUGHPUT]：tick 率 + 各客户端产出 fps / 空转占比 + 渲染耗时。

        与 [PRESSURE]（量"积压/丢帧"）正交——本行量"速率亏空"，并把亏空归到三侧之一：
        - viz-starved：本线程 tick 率 < 标称轮询率的 80% → 单线程被 GIL 争用/抢占饿着，
          还没轮询到就没得渲染。**worker 级信号，与客户端数、客户端存活时长全都无关**；
        - render-bound：单帧渲染峰值 ≥ 出帧间隔(1/out_target) → 渲染慢拖住产出；
        - supply-bound：tick 与渲染都正常但产出仍低 + 空转占比高 → 上游（throttle/推理）供帧慢。

        **出帧率的分母是该 run 在窗内的存活跨度，不是窗长**：run 中途才起来时按窗长算会把
        out_fps 算低而误报 supply-bound。历史上那道 `total >= expected_ticks*0.3` 的门就是
        为挡这种误报而设的补丁，但它拿 tick 数当"存活时长"的代理——只在 tick 率标称时成立，
        于是把 viz-starved（tick 数正好塌下去）一并咽掉了。分母改对后该门已删。

        **仅在三侧任一有压力时才打**，平稳时静默以免刷屏。
        日志失败绝不影响渲染热路径——整体 try/except 包裹。
        """
        try:
            if window <= 0:
                return
            poll_rate = 1.0 / self.tick_interval if self.tick_interval > 0 else 0.0
            # 出帧基准：期望出帧率（inference_fps），与轮询率解耦。过采样后 poll_rate > out_target，
            # 拿 poll_rate 当基准会让 out_fps(≈out_target) < poll_rate*0.8 恒真、告警常亮。
            out_target = self.output_fps if self.output_fps > 0 else poll_rate
            # render 的预算是「出帧间隔」(1/out_target)，非「轮询间隔」(tick)。过采样后 tick < 出帧间隔，
            # 单帧渲染只要塞进出帧间隔就不拖累出帧率（两次新推理间有 spare tick）；若拿 tick 当预算，
            # 本可支撑 out_target 的渲染会被误判 render-bound（把 supply 亏空错记到渲染头上）。
            budget_ms = (1000.0 / out_target) if out_target > 0 else (self.tick_interval * 1000.0)
            avg_ms = (self._render_time_sum / self._render_calls * 1000.0) if self._render_calls else 0.0
            max_ms = self._render_time_max * 1000.0
            render_bound = max_ms >= budget_ms and self._render_calls > 0

            # viz 线程健康度：窗内实际 tick 数 vs 按轮询率的应有 tick 数。这是 worker 级
            # 信号——没有客户端时循环照样空转到点，故它只反映线程本身抢不抢得到 CPU/GIL。
            expected_ticks = poll_rate * window
            tick_rate = self._tick_count / window
            tick_starved = poll_rate > 0 and tick_rate < poll_rate * _TICK_HEALTH_RATIO

            pressured = render_bound or tick_starved
            parts: List[str] = []
            win_end = self._win_start + window   # 调用点在 _reset_throughput_window 之前，_win_start 仍是本窗起点
            for cid in sorted(set(self._stat_rendered) | set(self._stat_stale)):
                rendered = self._stat_rendered.get(cid, 0)
                stale = self._stat_stale.get(cid, 0)
                total = rendered + stale
                # 分母 = 该 run 在窗内的存活跨度（首见→窗末）。未记首见（裸构造/直接喂计数
                # 的单测）回退窗长，保持旧口径。
                first_seen = self._first_seen.get(cid)
                span = window if first_seen is None else max(0.0, win_end - first_seen)
                out_fps = (rendered / span) if span > 0 else 0.0
                stale_pct = (stale / total * 100.0) if total else 0.0
                tag = ""
                # 存活够久（样本有效）且产出 < 期望出帧率 80% → 该 run 有压力。
                # 刚起的 run 只打数不判定：分母还没铺开，判了就是误报。
                if span >= _MIN_SPAN_SEC and out_fps < out_target * 0.8:
                    pressured = True
                    # 归因优先级：线程没转够 > 渲染慢 > 上游供帧慢。tick 都不够时产出低是
                    # 必然的，此时标 supply-bound 会把账错记到上游头上。
                    if tick_starved:
                        tag = " (viz-starved)"
                    elif render_bound:
                        tag = " (render-bound)"
                    else:
                        tag = " (supply-bound)"
                part = f"{cid} out={out_fps:.1f}fps stale={stale_pct:.0f}%"
                if span < window * 0.95:
                    part += f" span={span:.1f}s"   # 非整窗存活，标出来免得 out_fps 被误读
                parts.append(part + tag)

            if not pressured:
                return  # 平稳，静默

            logger.info(
                "[VIZ_THROUGHPUT] target=%.0ffps poll=%.0fHz ticks=%d/%.0f%s "
                "render=%.1fms(max %.1fms, budget %.0fms) || %s",
                out_target, poll_rate, self._tick_count, expected_ticks,
                " (viz-starved)" if tick_starved else "",
                avg_ms, max_ms, budget_ms,
                " | ".join(parts) if parts else "(none)",
            )
        except Exception as e:
            logger.debug("[VisualizationWorker] throughput snapshot failed: %s", e)
