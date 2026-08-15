"""清洗阶段时序算子：CleanOperator（TemporalOperator 子类）。

订阅 clean_large / clean_small 流，按 model_input_fps 重采样后喂时序模型出动作分类。
同业务点的流源 Detector 见 detection/impl/clean.py，离线分割见 offline/infer/impl/clean.py。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import torch

from app.services.inference.temporal.operator import TemporalOperator
from app.domain.alarm import Alarm
from app.domain.detection import FrameFeature


class CleanOperator(TemporalOperator):
    def __init__(
        self,
        name: str,
        subscribes: List[str],
        window_seconds: float,
        model_path: str,
        actions: Dict[int, str],
        objects: Dict[int, str],
        model_input_fps: float,
    ) -> None:
        super().__init__(
            name=name,
            subscribes=subscribes,
            window_seconds=window_seconds,
            model_path=model_path,
            actions=actions,
            objects=objects,
            model_input_fps=model_input_fps,
        )
        self._sm = {
            "last_ts": 0.0,
            "latest_action": 0,
        }

    def analyze(self, windows: List[FrameFeature]) -> None:
        """消费帧窗、推进 self._sm。windows: 帧级 FrameFeature 快照(按 ts 升序，多流已对齐)。"""
        aligned_frames = self._clip(windows)
        if not aligned_frames:
            return
        self._advance(aligned_frames)

    def judge(self) -> Tuple[List[str], List[Alarm]]:
        """读 self._sm 出 (overlay 文案, 实时告警)。"""
        events = [f"Action: {self._action_name(self._sm['latest_action'])}"]
        alarms = []
        return events, alarms

    def _advance(self, aligned_frames: List[FrameFeature]) -> None:
        """推进 self._sm。aligned_frames: 对齐后的窗口快照列表。"""
        last_ts = self._sm["last_ts"]
        new_frames = [f for f in aligned_frames if f.ts > last_ts]
        # 忽略新帧为空的情况
        if not new_frames:
            return

        # 入模前按 ts 重采样到模型契约帧率（检测采样率 → model_input_fps），消 train/serve skew。
        # 新帧门 / last_ts 推进仍基于完整 aligned_frames，重采样只决定喂给模型的时间轴密度。
        model_frames = self._resample_by_ts(aligned_frames)
        features = self._adapt_to_features(model_frames)
        if features.numel() == 0:
            return

        logits = self.infer(features)
        if logits is None:
            return
        self._sm["latest_action"] = logits[-1, :].argmax().item()
        self._sm["last_ts"] = new_frames[-1].ts

    def _adapt_to_features(self, aligned_frames: List[FrameFeature]) -> torch.Tensor:
        """将窗口快照转换为特征矩阵。

        同一原始帧的多流检测结果合并到同一个 feature vector 中。

        特征格式：(T, input_dim)，其中 input_dim = num_objects * 6
        每个物体的特征向量为 (nums, cx, cy, w, h, area)
        其中 `nums` 是同类别物体数量，其他特征归一化到 [0, 1] 范围。
        """

        if not aligned_frames:
            return torch.tensor([])

        features = []
        num_features_per_object = 6

        for aligned in aligned_frames:
            # 每帧一行特征：异常帧（无数据 / 推理失败 / 尺寸非法 / 无检测）统一留全零行，
            # 保证特征行数与 aligned_frames 严格一一对应，时间轴不缺帧。
            feature = [[0.0] * num_features_per_object for _ in range(self.num_objects)]

            # 帧级分辨率（同帧各流同值）读一次：缺失/非法则本帧留全零行，不进 by_source。
            width, height = aligned.frame_width, aligned.frame_height
            if not width or not height or width <= 0 or height <= 0:
                features.append(feature)
                continue

            for frame in aligned.by_source.values():
                # 跳过检测层推理失败的帧（不参与特征，留零贡献）
                if not frame.success:
                    continue

                if not frame.detections:
                    # 忽略无检测结果的帧
                    continue

                for detection in frame.detections:
                    bbox = detection.bbox
                    if bbox is None:
                        # 忽略未检测到的物体
                        continue
                    x1, y1, x2, y2 = bbox

                    # 确保 bbox 不超出图像边界
                    x1 = max(0, min(width, x1))
                    y1 = max(0, min(height, y1))
                    x2 = max(0, min(width, x2))
                    y2 = max(0, min(height, y2))
                    if x1 >= x2 or y1 >= y2:
                        # 忽略无效 bbox
                        continue

                    # 计算特征
                    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                    w, h = x2 - x1, y2 - y1
                    area = w * h

                    # 归一化特征
                    cx, cy = cx / width, cy / height
                    w, h = w / width, h / height
                    area = area / (width * height)

                    # 更新特征向量
                    # TODO: 当前实现为直接取代旧值，并未完全处理同类多实例的情况
                    object_id = self._object_id(detection.class_name)
                    if 0 <= object_id < self.num_objects:
                        count = feature[object_id][0]
                        feature[object_id] = [count + 1, cx, cy, w, h, area]

            features.append(feature)

        features = torch.tensor(features, dtype=torch.float32).reshape(len(aligned_frames), -1)
        return features
