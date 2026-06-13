# 代码模板

四种任务形态各一份骨架。先用 SKILL.md 的"选模板"决策表定位，再来抄对应模板。所有签名照抄基类不要改。

- [模板 A — YOLO 检测任务（最常见，实时告警）](#模板-a)
- [模板 B — 无模型纯算法任务](#模板-b)
- [模板 C — 结算式告警任务](#模板-c)
- [模板 D — 长窗口 / 低频序列模型](#模板-d)

字段与告警语义见 [data-models.md](data-models.md)，YAML 装配见 [yaml-config.md](yaml-config.md)。

---

## 模板 A

**YOLO 检测任务 + 实时告警**。参考 [bubble.py](../../../../app/services/inference/workflows/bubble.py)。

```python
import logging
import time
from typing import Any, Dict, List, Tuple

import numpy as np

from app.services.inference.workflows.detector import YOLODetector
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.data_models import (
    AlarmInfo, AlarmType, DetectionOutput,
    VisualizationData, VisItem, VisualizationType,
)

logger = logging.getLogger(__name__)


# ====== 推理线程：Detector（无状态，多 Client 共享） ======

class XxxDetector(YOLODetector):
    def __init__(self, model_path: str, conf_threshold: float = 0.5,
                 iou_threshold: float = 0.45, enabled: bool = True):
        super().__init__(
            name="xxx",                 # ← 必须与 XxxAnalyzer.name 一致
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )

    def infer_batch(self, frames: List[np.ndarray],
                    contexts: List[Dict[str, Any]]) -> List[DetectionOutput]:
        try:
            outputs = self._run_yolo_batch(frames)          # 批量路径（热路径）
            for output in outputs:
                output.success = True
                output.xxx_detected = len(output.detections) > 0   # 业务字段
            return outputs
        except Exception as e:
            logger.error("[XxxDetector] Batch failed, fallback: %s", e, exc_info=True)
            results = []
            for f, c in zip(frames, contexts):              # fallback 逐帧
                try:
                    output = self.infer(f, c)
                    output.xxx_detected = len(output.detections) > 0   # 字段逻辑须与上面一致
                    results.append(output)
                except Exception as err:
                    results.append(DetectionOutput(
                        detections=[], metadata={"error": str(err)},
                        timestamp=time.time(), success=False, error=str(err),
                    ))
            return results

    def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
        items = [
            VisItem(
                bbox=det.bbox,
                label=f"{det.class_name} {det.confidence:.2f}",
                confidence=det.confidence,
                color=(0, 0, 255),       # BGR，红色
            )
            for det in output.detections
        ]
        detected = len(output.detections) > 0
        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text="Detected!" if detected else "Normal",
            status_color=(0, 0, 255) if detected else (0, 255, 0),
            status_position="top-left",   # top-left / top-right / bottom-left / bottom-right
        )


# ====== 时序线程：TemporalAnalyzer（有状态，每 Client 独立实例） ======

class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, consecutive_trigger: int = 3, name: str = "xxx"):
        super().__init__(name=name)            # ← 与 XxxDetector.name 一致
        self.consecutive_trigger = consecutive_trigger
        self._sm = {
            "last_ts": 0.0,        # 游标：已处理到的最新帧 timestamp
            "consecutive": 0,      # 连续命中帧计数（跨 tick 累积，命中 +1，未命中归 0）
            "alarming": False,     # 上升沿锁存
        }

    def analyze_temporal(self, window: List[DetectionOutput]) -> Tuple[List[str], List[AlarmInfo]]:
        if not window:
            return [], []

        # ① 游标推进：只处理上次 tick 之后的新帧，避免同一帧跨 tick 重复计数（见 SKILL.md §游标）
        last_ts = self._sm["last_ts"]
        new_frames = [f for f in window if f.timestamp > last_ts]
        for output in new_frames:
            if len(output.detections) > 0:
                self._sm["consecutive"] += 1
            else:
                self._sm["consecutive"] = 0
        if new_frames:
            self._sm["last_ts"] = new_frames[-1].timestamp

        # ② 算指标
        consecutive = self._sm["consecutive"]
        is_triggered = consecutive >= self.consecutive_trigger

        # ③ events（给前端 overlay）
        events = [f"xxx in {consecutive} consecutive frames"] if is_triggered else []

        # ④ 实时告警：上升沿触发，下降沿复位
        alarms: List[AlarmInfo] = []
        if is_triggered and not self._sm["alarming"]:
            self._sm["alarming"] = True
            alarms.append(AlarmInfo(
                alarm_type=AlarmType.PROCESS_VIOLATION,
                alarm_level="high",                 # low / medium / high / critical
                alarm_message="检测到 xxx 异常",
                metadata={"consecutive_frames": consecutive},
            ))
        elif not is_triggered and self._sm["alarming"]:
            self._sm["alarming"] = False

        return events, alarms
```

> 复杂指标（如 ByteTrack 跨帧追踪 + birth_rate）参考 [bubble.py](../../../../app/services/inference/workflows/bubble.py)：把"游标推进"和"算指标"拆成 `_advance` / `_compute_metric` 私有方法，逻辑更清晰。

---

## 模板 B

**无模型纯算法任务**。无 YOLO 依赖时，Detector 直接继承 `Detector` 并实现 `infer`。参考 [mock.py](../../../../app/services/inference/workflows/mock.py)。

```python
from app.services.inference.workflows.detector import Detector

class XxxDetector(Detector):
    def __init__(self, enabled: bool = True):
        super().__init__(name="xxx", enabled=enabled)

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        # 纯 numpy / time 计算 ...
        return DetectionOutput(
            detections=[...], metadata={"model": "xxx_algo"},
            timestamp=time.time(), success=True,
        )

    # infer_batch 可不 override（基类默认逐帧）；prepare_visualization_data 同模板 A
    # Analyzer 同模板 A
```

---

## 模板 C

**结算式告警任务**。实时阶段只产 events、**不报警**，任务结束时一次性裁决。参考 [bending.py](../../../../app/services/inference/workflows/bending.py)。

```python
class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, required_actions: int = 4, name: str = "xxx"):
        super().__init__(name=name)
        self.required_actions = required_actions
        self._sm = {"last_ts": 0.0, "action_count": 0}

    def analyze_temporal(self, window: List[DetectionOutput]) -> Tuple[List[str], List[AlarmInfo]]:
        if not window:
            return [], []
        # ... 推进游标、累加 self._sm["action_count"] ...
        events = [f"动作 {self._sm['action_count']}/{self.required_actions}"]
        return events, []          # 实时阶段不上报告警

    def finalize(self) -> List[AlarmInfo]:
        if self._sm["action_count"] < self.required_actions:
            return [AlarmInfo(
                alarm_type=AlarmType.PROCESS_VIOLATION,
                alarm_level="warning",
                alarm_message=f"动作不足：{self._sm['action_count']}/{self.required_actions}",
                metadata={"metric": self.name, "count": self._sm["action_count"]},
            )]
        return []
```

---

## 模板 D

**长窗口 / 低频序列模型**（动作分割、行为识别、时序定位）。

**适用场景**：模型不是逐帧独立判断，而是需要一段较长上下文（如 **30s**）才能有效推理，且**推理频率不需要高**。

**四条设计要点**（核心是"指标自管窗口"规则的实战，见 SKILL.md §游标）：

1. **拆成 主干 + 时序头**：per-frame backbone 放 **Detector**（无状态、多 Client 共享、`MultiModelWorkerPool` 自动组批），30s 时序头放 **Analyzer**（有状态、per-client）。Detector 把每帧特征塞进 `output.metadata["embedding"]`，**只缓存紧凑特征，不缓存原始帧**（30s 原始帧 = 几十 MB/client）。
2. **30s 窗口在 Analyzer 内部自管，绝不调全局 `_slide_window_seconds`**：Actor 固定 1Hz tick 排空 10s 全局缓冲，余量 10 倍，游标一帧不丢；Analyzer 在 `self._sm` 里自攒 30s 环形缓冲即可。调全局缓冲会拖累所有任务内存并破坏 `signals_10s` 语义。
3. **两层节流，互相独立**：① 主干抽帧频率用全局 `inference_decimation` / 低 `inference_fps`（动作分割 1–3fps 通常够）；② 时序头**在 analyzer 内部用计时器自节流**——每 tick 只做"游标推进 + 追加特征"（廉价），每 K 秒才真正跑一次重模型（昂贵），两次之间复用上次分割结果。这样 tick 频率（1Hz）与分割频率（每 K 秒）解耦。
4. **warm-up 守卫**：缓冲不足 30s（任务刚开始）时只产"预热中"event，不分割、不告警。

```python
class ActionSegDetector(YOLODetector):          # 主干是 YOLO 系则继承 YOLODetector，否则继承 Detector
    def infer_batch(self, frames, contexts):
        outputs = self._run_backbone_batch(frames)
        for o, emb in zip(outputs, embs):
            o.metadata["embedding"] = emb       # 紧凑特征塞 metadata，不缓存原始帧
        return outputs


class ActionSegAnalyzer(TemporalAnalyzer):
    def __init__(self, window_seconds=30.0, seg_interval=5.0, name="action_seg"):
        super().__init__(name=name)
        self.window_seconds = window_seconds    # ← 指标窗口，自管，与全局 slide_window 无关
        self.seg_interval = seg_interval
        self._sm = {
            "last_ts": 0.0,                     # 游标
            "feat_buf": [],                     # [(ts, embedding)] 自管 30s 环形缓冲
            "last_seg_ts": 0.0,                 # 时序头节流计时器
            "last_segments": [],                # 两次推理之间复用
            "alarming": False,
        }

    def analyze_temporal(self, window):
        if not window:
            return [], []

        # ① 游标推进：只取新帧，追加特征，按 window_seconds 裁剪（廉价，每 tick 都做）
        last_ts = self._sm["last_ts"]
        for f in (x for x in window if x.timestamp > last_ts):
            self._sm["feat_buf"].append((f.timestamp, f.metadata.get("embedding")))
            self._sm["last_ts"] = f.timestamp
        cutoff = window[-1].timestamp - self.window_seconds
        self._sm["feat_buf"] = [(t, e) for t, e in self._sm["feat_buf"] if t >= cutoff]

        # ② warm-up 守卫：不够 window_seconds 不分割
        buf = self._sm["feat_buf"]
        span = buf[-1][0] - buf[0][0] if len(buf) >= 2 else 0.0
        if span < self.window_seconds:
            return [f"segmenting warmup {span:.0f}/{self.window_seconds:.0f}s"], []

        # ③ 节流：每 seg_interval 秒才跑一次重模型（昂贵）
        now = window[-1].timestamp
        if now - self._sm["last_seg_ts"] >= self.seg_interval:
            self._sm["last_segments"] = self._run_seg_head(buf)
            self._sm["last_seg_ts"] = now

        # ④ 基于分割结果产 events / 告警（实时上升沿 或 留到 finalize 结算）
        return self._evaluate(self._sm["last_segments"])
```

> **两个岔路按需选**：① 模型可拆 backbone+head（推荐，省内存）vs 整体吃原始帧（Detector 退化为降采样取帧，内存重，强降分辨率）；② 告警实时（违规动作段上升沿）vs 结算（任务结束核对动作序列完整性，`realtime: false` + `finalize()`，动作分割多偏此类）。
