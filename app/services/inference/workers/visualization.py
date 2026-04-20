"""visualization.py - 可视化工作线程池（定时拉取架构）。

职责：
- 按固定间隔（tick_interval）轮询所有活跃客户端
- 从 ClientQueues 主动拉取三要素：
  - cq.get_latest_inference()  → 原子推理快照（所有 task 同帧一致）
  - cq.get_latest_frame()      → 最新原始帧
  - cq.get_latest_temporal()   → 最新时序事件
- 绘制检测框、标注、文字信息到最新帧上
- 写回 ClientQueues（ca_processed + _latest_rendered）
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import subprocess

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.services.inference.data_models import (
    DetectionOutput,
    VisualizationData,
    VisItem,
    VisualizationType,
)

from app.models.frame import FrameData
from app.services.client import client_manager
from app.services.inference.models import InferenceResult
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class VisualizationWorker:
    """可视化工作线程（定时拉取模式）。

    独立于 TemporalWorker，按自己的节奏（tick_interval）遍历所有活跃客户端，
    从 ClientQueues 拉取原子推理快照 + 最新帧 + 时序事件，渲染后写回。
    """

    def __init__(
        self,
        stop_event: threading.Event,
        tick_interval: float = 1.0 / 15,  # ~15 FPS
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化工作线程。

        Args:
            stop_event: 停止事件
            tick_interval: 拉取间隔（秒），默认 ~15 FPS
            worker_id: 工作线程ID（用于调试）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.stop_event = stop_event
        self.tick_interval = tick_interval
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}
        self.fixed_visualizer = FixedVisualizer()

        # 去重：记录每个客户端上次渲染的推理时间戳，避免重复渲染同一帧
        self._last_rendered_ts: Dict[str, float] = {}

    def run(self):
        """工作循环：固定间隔轮询所有客户端。"""
        logger.debug(
            "[VisualizationWorker-%d] Started (tick=%.3fs, ~%.0f FPS)",
            self.worker_id, self.tick_interval, 1.0 / self.tick_interval,
        )

        while not self.stop_event.is_set():
            tick_start = time.time()
            try:
                self._tick()
            except Exception as e:
                logger.error(
                    "[VisualizationWorker-%d] Tick exception: %s",
                    self.worker_id, e, exc_info=True,
                )

            # 睡眠至下一个 tick
            elapsed = time.time() - tick_start
            sleep_time = max(0, self.tick_interval - elapsed)
            if sleep_time > 0:
                self.stop_event.wait(sleep_time)

        logger.debug("[VisualizationWorker-%d] Stopped", self.worker_id)

    def _tick(self):
        """一次轮询：遍历所有活跃客户端执行可视化。"""
        all_clients = client_manager.get_all_clients()
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
        inference: Optional[InferenceResult] = cq.get_latest_inference()
        if inference is None:
            return

        # 2. 去重：跳过已渲染过的同一推理结果
        last_ts = self._last_rendered_ts.get(client_id, 0.0)
        if inference.timestamp <= last_ts:
            return

        # 3. 获取最新原始帧
        frame = cq.get_latest_frame()
        if frame is None:
            return

        # 4. 获取最新时序事件
        events = cq.get_latest_temporal()

        # 5. 渲染
        stage = inference.stage
        annotated_frame = self._render(frame, stage, inference.result, events)

        # 6. 写回
        frame_data = FrameData(
            timestamp=inference.timestamp,
            frame=annotated_frame,
            inference_result=inference.result,
        )
        cq.append_ca_processed(frame_data)
        cq.set_latest_rendered(frame_data)

        # 7. 更新去重时间戳
        self._last_rendered_ts[client_id] = inference.timestamp

    def _render(
        self,
        frame: np.ndarray,
        stage: str,
        detection_results: Dict[str, DetectionOutput],
        events: List[str],
    ) -> np.ndarray:
        """使用固定渲染器进行可视化。

        Args:
            frame: 原始帧
            stage: 当前阶段
            detection_results: 推理结果 {task_name: DetectionOutput}（同帧原子快照）
            events: 时序事件列表
        """
        try:
            # 获取当前 stage 的 tasks（用于调用 prepare_visualization_data）
            stage_cfg = self.stage_configs.get(stage, {})
            tasks = stage_cfg.get("models", [])

            if not tasks:
                return frame.copy()

            vis_data_list: List[VisualizationData] = []

            for task in tasks:
                detection_output = detection_results.get(task.name)

                if not isinstance(detection_output, DetectionOutput):
                    continue

                vis_data = task.prepare_visualization_data(detection_output)
                vis_data_list.append(vis_data)

            # 使用固定渲染器渲染
            annotated_frame = self.fixed_visualizer.render(
                frame=frame.copy(),
                vis_data_list=vis_data_list,
                stage=stage,
                temporal_events=events,
            )

            return annotated_frame

        except Exception as e:
            logger.error("[VisualizationWorker] Render failed: %s", e, exc_info=True)
            return frame.copy()


class VisualizationWorkerPool:
    """可视化线程池（定时拉取模式，单线程）。

    单线程理由：单帧渲染 ~5ms，15 FPS × 10 clients = 50ms/67ms，单线程足够。
    单线程避免了多线程竞争同一客户端的问题。
    """

    def __init__(
        self,
        target_fps: float = 15,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化线程池。

        Args:
            target_fps: 目标可视化帧率（默认 15 FPS）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.target_fps = target_fps
        self.stage_configs = stage_configs or {}

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def start(self):
        """启动工作线程。"""
        tick_interval = 1.0 / self.target_fps

        worker = VisualizationWorker(
            stop_event=self._stop_event,
            tick_interval=tick_interval,
            worker_id=0,
            stage_configs=self.stage_configs,
        )

        self._worker_thread = threading.Thread(
            target=guarded_run,
            args=(worker.run, self._stop_event, "VisualizationWorker-0"),
            daemon=True,
            name="VisualizationWorker-0",
        )
        self._worker_thread.start()

        logger.info(
            "[VisualizationWorkerPool] Started (target_fps=%.0f, tick=%.3fs)",
            self.target_fps, tick_interval,
        )

    def stop(self):
        """停止工作线程。"""
        self._stop_event.set()

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)

        logger.debug("[VisualizationWorkerPool] Stopped")


class FixedVisualizer:
    """固定可视化渲染器

    根据 Task 提供的 VisualizationData 渲染视频帧，无需针对每个任务编写可视化代码。
    支持多种可视化类型：BBox、Segmentation、Keypoint
    """

    # 类级别缓存字体，避免重复加载
    _font_cache: Dict[int, Any] = {}

    @classmethod
    def _get_font(cls, size: int = 20) -> ImageFont.FreeTypeFont:
        """获取支持中文的粗体字体（带缓存），兼容 Windows / Ubuntu。"""
        if size not in cls._font_cache:
            # 优先粗体，再回退常规体（Windows + Ubuntu 常见路径）
            font_paths = [
                # Windows
                "C:/Windows/Fonts/msyhbd.ttc",     # 微软雅黑 Bold
                "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
                "C:/Windows/Fonts/simsun.ttc",     # 宋体
                # Ubuntu / Debian (fonts-noto-cjk)
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
                # Ubuntu (fonts-wqy-zenhei / fonts-wqy-microhei)
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                # Ubuntu (fonts-droid-fallback)
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            ]
            for fp in font_paths:
                try:
                    cls._font_cache[size] = ImageFont.truetype(fp, size)
                    break
                except (IOError, OSError):
                    continue
            else:
                # 兜底：用 fc-match 动态查找（仅 Linux）
                cls._font_cache[size] = cls._fc_match_font(size)
        return cls._font_cache[size]

    @classmethod
    def _fc_match_font(cls, size: int) -> Any:
        """Linux 兜底：通过 fc-match 查找系统中文字体。"""
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", ":lang=zh"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                return ImageFont.truetype(result.stdout.strip(), size)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        logger.warning("[FixedVisualizer] 未找到中文字体，中文可能无法正常显示")
        return ImageFont.load_default()

    @staticmethod
    def _flush_texts(frame: np.ndarray, text_cmds: list) -> None:
        """批量绘制所有文本，仅做一次 BGR↔PIL 转换。

        Args:
            frame: BGR numpy 数组（原地修改）
            text_cmds: [(org, text, font_size, bgr_color, anchor), ...]
        """
        if not text_cmds:
            return
        pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_img)
        for org, text, font_size, color, anchor in text_cmds:
            font = FixedVisualizer._get_font(font_size)
            rgb_color = (color[2], color[1], color[0])
            draw.text(org, text, font=font, fill=rgb_color, anchor=anchor)
        frame[:] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    @staticmethod
    def _get_text_size(text: str, font_size: int) -> tuple:
        """获取文本渲染尺寸 (width, height)。"""
        font = FixedVisualizer._get_font(font_size)
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    @staticmethod
    def _draw_rounded_rect(
        frame: np.ndarray,
        pt1: tuple,
        pt2: tuple,
        color: tuple,
        radius: int = 6,
        alpha: float = 0.7,
    ) -> None:
        """绘制半透明圆角矩形背景。"""
        x1, y1 = pt1
        x2, y2 = pt2
        overlay = frame.copy()
        # 填充主体矩形 + 四角圆形实现圆角效果
        cv2.rectangle(overlay, (x1 + radius, y1), (x2 - radius, y2), color, -1)
        cv2.rectangle(overlay, (x1, y1 + radius), (x2, y2 - radius), color, -1)
        for cx, cy in [(x1 + radius, y1 + radius), (x2 - radius, y1 + radius),
                       (x1 + radius, y2 - radius), (x2 - radius, y2 - radius)]:
            cv2.circle(overlay, (cx, cy), radius, color, -1)
        cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, dst=frame)

    def render(
        self,
        frame: np.ndarray,
        vis_data_list: List["VisualizationData"],
        stage: str,
        temporal_events: Optional[List[str]] = None,
    ) -> np.ndarray:
        """渲染所有Task的可视化数据

        Args:
            frame: 原始帧
            vis_data_list: Task提供的可视化数据列表
            stage: 当前阶段（如"LEAK"）
            temporal_events: 时序事件列表（如["连续3帧检测到气泡"]）

        Returns:
            可视化后的帧
        """
        if frame is None:
            return frame

        annotated = frame.copy()
        # 文本指令收集器：所有 _draw_* 方法往这里追加，最后统一 flush
        text_cmds: list = []

        for vis_data in vis_data_list:
            if vis_data.type == VisualizationType.BBOX:
                self._draw_bboxes(annotated, vis_data.items, text_cmds)
            elif vis_data.type == VisualizationType.MASK:
                self._draw_masks(annotated, vis_data.items, text_cmds)
            elif vis_data.type == VisualizationType.KEYPOINT:
                self._draw_keypoints(annotated, vis_data.items)

            self._draw_status_bar(
                annotated,
                vis_data.status_text,
                vis_data.status_color,
                vis_data.status_position,
                text_cmds,
            )

        self._draw_global_info(annotated, stage, temporal_events, text_cmds)

        # 一次性 BGR→PIL→BGR 绘制所有文本
        self._flush_texts(annotated, text_cmds)
        return annotated

    def _draw_bboxes(self, frame: np.ndarray, items: List["VisItem"], text_cmds: list):
        FONT_SIZE = 16
        PAD = 4
        for item in items:
            if item.bbox is None:
                continue
            x1, y1, x2, y2 = item.bbox
            cv2.rectangle(frame, (x1, y1), (x2, y2), item.color, 2)
            if item.label:
                label_w, label_h = self._get_text_size(item.label, FONT_SIZE)
                box_cx = (x1 + x2) // 2
                bg_w = label_w + PAD * 2
                bg_h = label_h + PAD * 2
                bg_top = max(y1 - bg_h - 2, 0)
                bg_left = max(box_cx - bg_w // 2, 0)
                self._draw_rounded_rect(
                    frame,
                    (bg_left, bg_top),
                    (bg_left + bg_w, bg_top + bg_h),
                    item.color, radius=4, alpha=0.85,
                )
                text_cmds.append((
                    (bg_left + bg_w // 2, bg_top + bg_h // 2),
                    item.label, FONT_SIZE, (255, 255, 255), "mm",
                ))

    def _draw_masks(self, frame: np.ndarray, items: List["VisItem"], text_cmds: list):
        for item in items:
            if item.mask is None:
                continue
            colored_mask = np.zeros_like(frame, dtype=np.uint8)
            colored_mask[item.mask > 0] = item.color
            frame[:] = cv2.addWeighted(frame, 0.5, colored_mask, 0.5, 0)
            contours, _ = cv2.findContours(
                item.mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            cv2.drawContours(frame, contours, -1, item.color, 2)
            if contours and item.label:
                x, y, w, _ = cv2.boundingRect(contours[0])
                text_cmds.append((
                    (x + w // 2, y - 10),
                    item.label, 16, item.color, "mb",
                ))

    def _draw_keypoints(self, frame: np.ndarray, items: List["VisItem"]):
        for item in items:
            if item.keypoints is None:
                continue
            for kp in item.keypoints:
                if isinstance(kp, (list, tuple)) and len(kp) >= 2:
                    x, y = int(kp[0]), int(kp[1])
                    cv2.circle(frame, (x, y), 5, item.color, -1)
                    cv2.circle(frame, (x, y), 6, (255, 255, 255), 1)
            if len(item.keypoints) > 1:
                for i in range(len(item.keypoints) - 1):
                    kp1, kp2 = item.keypoints[i], item.keypoints[i + 1]
                    if (isinstance(kp1, (list, tuple)) and len(kp1) >= 2
                            and isinstance(kp2, (list, tuple)) and len(kp2) >= 2):
                        cv2.line(
                            frame,
                            (int(kp1[0]), int(kp1[1])),
                            (int(kp2[0]), int(kp2[1])),
                            item.color, 2,
                        )

    def _draw_status_bar(
        self,
        frame: np.ndarray,
        text: str,
        color: tuple,
        position: str = "top-right",
        text_cmds: Optional[list] = None,
    ):
        FONT_SIZE = 20
        height, width = frame.shape[:2]
        padding = 12
        text_w, text_h = self._get_text_size(text, FONT_SIZE)
        bg_w = text_w + padding * 2
        bg_h = text_h + padding * 2
        if position == "top-right":
            bg_left, bg_top = width - bg_w - 8, 8
        elif position == "top-left":
            bg_left, bg_top = 8, 8
        elif position == "bottom-right":
            bg_left, bg_top = width - bg_w - 8, height - bg_h - 8
        else:  # bottom-left
            bg_left, bg_top = 8, height - bg_h - 8
        self._draw_rounded_rect(
            frame, (bg_left, bg_top), (bg_left + bg_w, bg_top + bg_h),
            (0, 0, 0), radius=8, alpha=0.6,
        )
        if text_cmds is not None:
            text_cmds.append((
                (bg_left + bg_w // 2, bg_top + bg_h // 2),
                text, FONT_SIZE, color, "mm",
            ))

    def _draw_global_info(
        self,
        frame: np.ndarray,
        stage: str,
        temporal_events: Optional[List[str]] = None,
        text_cmds: Optional[list] = None,
    ):
        FONT_SIZE = 18
        height, width = frame.shape[:2]
        padding = 10

        # 左下角信息栏：Stage + 时间
        info_line = f"Stage: {stage}  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        info_w, info_h = self._get_text_size(info_line, FONT_SIZE)
        bg_w = info_w + padding * 2
        bg_h = info_h + padding * 2
        bg_top = height - bg_h - 8
        self._draw_rounded_rect(
            frame, (8, bg_top), (8 + bg_w, bg_top + bg_h),
            (0, 0, 0), radius=8, alpha=0.6,
        )
        if text_cmds is not None:
            text_cmds.append((
                (8 + bg_w // 2, bg_top + bg_h // 2),
                info_line, FONT_SIZE, (255, 255, 255), "mm",
            ))

        # 右下角事件栏
        if temporal_events:
            events_text = " | ".join(temporal_events[:2])
            event_w, event_h = self._get_text_size(events_text, FONT_SIZE)
            ebg_w = event_w + padding * 2
            ebg_h = event_h + padding * 2
            ebg_top = height - ebg_h - 8
            ebg_left = width - ebg_w - 8
            self._draw_rounded_rect(
                frame, (ebg_left, ebg_top), (ebg_left + ebg_w, ebg_top + ebg_h),
                (0, 0, 0), radius=8, alpha=0.6,
            )
            if text_cmds is not None:
                text_cmds.append((
                    (ebg_left + ebg_w // 2, ebg_top + ebg_h // 2),
                    events_text, FONT_SIZE, (0, 165, 255), "mm",
                ))
