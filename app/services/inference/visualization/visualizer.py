"""visualizer.py - 固定可视化渲染器（FixedVisualizer）。

根据 Task 提供的 RenderSpec 渲染视频帧，无需针对每个任务编写可视化代码。
支持多种可视化类型：BBox、Segmentation、Keypoint。

纯渲染器：无线程、无队列、无 client 概念，只吃 frame + RenderSpec 出标注帧。
由 VisualizationWorker（同层 worker.py）持有并调用。
"""

import logging
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.domain.render import RenderItem, RenderSpec, RenderType

logger = logging.getLogger(__name__)


class FixedVisualizer:
    """固定可视化渲染器

    根据 Task 提供的 RenderSpec 渲染视频帧，无需针对每个任务编写可视化代码。
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
        """绘制半透明圆角矩形背景（仅在矩形包围盒 ROI 上拷贝/混合）。

        行为等价于"整帧 copy + 整帧 addWeighted"：矩形外 overlay==frame，addWeighted
        还原原像素（纯浪费算力）。改只在 ROI 上操作，单帧多框时削掉渲染尾延迟尖峰。
        """
        x1, y1 = pt1
        x2, y2 = pt2
        h, w = frame.shape[:2]
        # clip 包围盒到帧边界；空 ROI 直接返回。cv2.rectangle/circle 端点闭区间，
        # 形状覆盖像素 x1..x2、y1..y2（含端点），故 slice 上界取 x2+1 / y2+1。
        rx1, ry1 = max(0, x1), max(0, y1)
        rx2, ry2 = min(w, x2 + 1), min(h, y2 + 1)
        if rx2 <= rx1 or ry2 <= ry1:
            return
        roi = frame[ry1:ry2, rx1:rx2]
        overlay = roi.copy()
        # 形状坐标平移到 ROI 局部坐标系（减去 ROI 原点）；越界部分由 cv2 自动裁剪
        ox, oy = rx1, ry1
        cv2.rectangle(overlay, (x1 + radius - ox, y1 - oy), (x2 - radius - ox, y2 - oy), color, -1)
        cv2.rectangle(overlay, (x1 - ox, y1 + radius - oy), (x2 - ox, y2 - radius - oy), color, -1)
        for cx, cy in [(x1 + radius, y1 + radius), (x2 - radius, y1 + radius),
                       (x1 + radius, y2 - radius), (x2 - radius, y2 - radius)]:
            cv2.circle(overlay, (cx - ox, cy - oy), radius, color, -1)
        cv2.addWeighted(overlay, alpha, roi, 1 - alpha, 0, dst=roi)

    def render(
        self,
        frame: np.ndarray,
        vis_data_list: List["RenderSpec"],
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
            if vis_data.type == RenderType.BBOX:
                self._draw_bboxes(annotated, vis_data.items, text_cmds)
            elif vis_data.type == RenderType.MASK:
                self._draw_masks(annotated, vis_data.items, text_cmds)
            elif vis_data.type == RenderType.KEYPOINT:
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

    def _draw_bboxes(self, frame: np.ndarray, items: List["RenderItem"], text_cmds: list):
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

    def _draw_masks(self, frame: np.ndarray, items: List["RenderItem"], text_cmds: list):
        for item in items:
            if item.mask is None:
                continue
            mask_u8 = item.mask.astype(np.uint8)
            contours, _ = cv2.findContours(
                mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            if not contours:
                continue
            # 仅在 mask 包围盒 ROI 上着色混合，且只染 mask 像素本身——避免整帧 alpha 混合
            # 的尾延迟尖峰，同时修掉旧实现"对 mask 外区域也按 0.5 压暗整帧"的副作用。
            mx, my, mw, mh = cv2.boundingRect(mask_u8)
            roi = frame[my:my + mh, mx:mx + mw]
            mask_roi = mask_u8[my:my + mh, mx:mx + mw] > 0
            if mask_roi.any():
                colored = np.empty_like(roi)
                colored[:] = item.color
                blended = cv2.addWeighted(roi, 0.5, colored, 0.5, 0)
                roi[mask_roi] = blended[mask_roi]
            cv2.drawContours(frame, contours, -1, item.color, 2)
            if item.label:
                x, y, w, _ = cv2.boundingRect(contours[0])
                text_cmds.append((
                    (x + w // 2, y - 10),
                    item.label, 16, item.color, "mb",
                ))

    def _draw_keypoints(self, frame: np.ndarray, items: List["RenderItem"]):
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
