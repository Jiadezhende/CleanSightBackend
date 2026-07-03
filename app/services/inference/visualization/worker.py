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

from app.domain.detection import FrameDetections
from app.domain.render import RenderSpec
from app.domain.frame import Frame
from app.services.inference.naming import get_stage_alias
from app.services.client import client_manager
from app.services.inference.models import FrameInference
from app.services.inference.visualization.visualizer import FixedVisualizer

logger = logging.getLogger(__name__)


class VisualizationWorker:
    """可视化工作线程（定时拉取模式）。

    独立于 TemporalWorker，按自己的节奏（tick_interval）遍历所有活跃客户端，
    从 ClientQueues 拉取原子推理快照 + 最新帧 + 时序事件，渲染后写回。
    """

    def __init__(
        self,
        stop_event: threading.Event,
        tick_interval: float = 1.0 / 20,  # 兜底 ~20 FPS；实际由 pool 按 settings.inference_fps 注入
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化工作线程。

        Args:
            stop_event: 停止事件
            tick_interval: 拉取间隔（秒），由 pool 按 settings.inference_fps 注入
            worker_id: 工作线程ID（用于调试）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.stop_event = stop_event
        self.tick_interval = tick_interval
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}
        self.fixed_visualizer = FixedVisualizer()

        # 去重：记录每个 run(键=task_id) 上次渲染的推理时间戳，避免重复渲染同一帧
        self._last_rendered_ts: Dict[int, float] = {}

        # 吞吐量观测（[VIZ_THROUGHPUT]）：每 ~10s 评估一次，仅在产出明显低于目标
        # 时才打印，平稳时静默以免刷屏。目的：定位 processed 成帧率不足是
        # "上游供帧慢"(supply-bound) 还是"单帧渲染慢"(render-bound)。
        self._win_start: float = 0.0          # 当前统计窗起点（首轮 run 内用 time.time 初始化）
        self._eval_interval: float = 10.0     # 评估窗长（秒）
        self._stat_rendered: Dict[str, int] = defaultdict(int)  # 实际渲染（=新推理结果）帧数
        self._stat_stale: Dict[str, int] = defaultdict(int)     # 有推理但无新结果而空转的 tick 数
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
                self._tick()
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

    def _tick(self):
        """一次轮询：遍历所有活跃客户端执行可视化。"""
        all_clients = client_manager.snapshot()
        for client_id, cq in all_clients.items():
            try:
                self._process_client(client_id, cq)
            except Exception as e:
                logger.error(
                    "[VisualizationWorker-%d] Error processing client %s: %s",
                    self.worker_id, client_id, e, exc_info=True,
                )

        # 自清理：移除已不在 ClientManager 中的客户端去重记录（防止内存泄漏）
        stale_ids = self._last_rendered_ts.keys() - all_clients.keys()
        for stale_id in stale_ids:
            del self._last_rendered_ts[stale_id]

    def _process_client(self, client_id: str, cq) -> None:
        """处理单个客户端的可视化。"""
        # 1. 原子读取推理快照（所有 task 同帧一致）
        inference: Optional[FrameInference] = cq.get_latest_inference()
        if inference is None:
            return

        # 2. 去重：跳过已渲染过的同一推理结果
        last_ts = self._last_rendered_ts.get(client_id, 0.0)
        if inference.timestamp <= last_ts:
            # 有推理快照但无新结果 → tick 空转。占比高即"上游供帧慢"的直接信号。
            self._stat_stale[client_id] += 1
            return

        # 3. 获取最新原始帧
        frame = cq.get_latest_frame()
        if frame is None:
            return

        # 4. 获取最新时序事件
        events = cq.get_latest_temporal()

        # 5. 渲染（计时，用于判定是否 render-bound）
        stage = inference.stage
        t0 = time.perf_counter()
        annotated_frame = self._render(frame, stage, inference.detections, events)
        dt = time.perf_counter() - t0
        self._render_time_sum += dt
        self._render_calls += 1
        if dt > self._render_time_max:
            self._render_time_max = dt

        # 6. 写回
        frame_data = Frame(
            timestamp=inference.timestamp,
            frame=annotated_frame,
            inference_result=inference.detections,
        )
        cq.append_ca_processed(frame_data)
        cq.set_latest_rendered(frame_data)

        # 7. 更新去重时间戳 + 计成帧数（实际渲染帧数 = processed 真实成帧率）
        self._last_rendered_ts[client_id] = inference.timestamp
        self._stat_rendered[client_id] += 1

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
        self._stat_rendered.clear()
        self._stat_stale.clear()
        self._render_time_sum = 0.0
        self._render_time_max = 0.0
        self._render_calls = 0

    def _log_throughput_snapshot(self, window: float) -> None:
        """有压力时打一条 [VIZ_THROUGHPUT]：各客户端产出 fps / 空转占比 + 渲染耗时。

        与 [INFER_PRESSURE]（量"积压/丢帧"）正交——本行量"速率亏空"：processed 成帧率
        是否低于目标，并据渲染耗时是否逼近 tick 预算，自动判定瓶颈侧：
        - render-bound：单帧渲染峰值 ≥ tick 预算 → 渲染慢拖住产出；
        - supply-bound：渲染很快但产出仍低 + 空转占比高 → 上游（throttle/推理）供帧慢。

        **仅在有客户端产出明显低于目标、或渲染逼近预算时才打**，平稳时静默以免刷屏。
        日志失败绝不影响渲染热路径——整体 try/except 包裹。
        """
        try:
            if window <= 0:
                return
            target = 1.0 / self.tick_interval if self.tick_interval > 0 else 0.0
            budget_ms = self.tick_interval * 1000.0
            avg_ms = (self._render_time_sum / self._render_calls * 1000.0) if self._render_calls else 0.0
            max_ms = self._render_time_max * 1000.0
            render_bound = max_ms >= budget_ms and self._render_calls > 0

            pressured = render_bound
            parts: List[str] = []
            # 期望窗内最多 tick 数（供判定"是否真有推理流"，过滤近空闲流的误报）
            expected_ticks = target * window
            for cid in sorted(set(self._stat_rendered) | set(self._stat_stale)):
                rendered = self._stat_rendered.get(cid, 0)
                stale = self._stat_stale.get(cid, 0)
                out_fps = rendered / window
                total = rendered + stale
                stale_pct = (stale / total * 100.0) if total else 0.0
                tag = ""
                # 有实际推理流（tick 数足够）且产出 < 目标 80% → 该客户端有压力
                if total >= expected_ticks * 0.3 and out_fps < target * 0.8:
                    pressured = True
                    tag = " (render-bound)" if render_bound else " (supply-bound)"
                parts.append(
                    f"{cid} out={out_fps:.1f}fps stale={stale_pct:.0f}%{tag}"
                )

            if not pressured:
                return  # 平稳，静默

            logger.info(
                "[VIZ_THROUGHPUT] target=%.0ffps render=%.1fms(max %.1fms, budget %.0fms) || %s",
                target, avg_ms, max_ms, budget_ms,
                " | ".join(parts) if parts else "(none)",
            )
        except Exception as e:
            logger.debug("[VisualizationWorker] throughput snapshot failed: %s", e)
