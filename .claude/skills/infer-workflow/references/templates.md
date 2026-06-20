# 代码模板

四种任务形态各一份骨架，每份都含 **Detector(L1) + TemporalAnalyzer(L3) + Judge(L4)** 三件套。先用 SKILL.md 的"选模板"决策表定位，再来抄对应模板。**所有签名照抄基类不要改，三个类的 `name` 必须一致。**

- [模板 A — YOLO 检测任务（最常见，实时告警）](#模板-a)
- [模板 B — 无模型纯算法任务](#模板-b)
- [模板 C — 结算式告警任务](#模板-c)
- [模板 D — 在线轻量因果序列模型](#模板-d)

字段与告警语义见 [data-models.md](data-models.md)，YAML 装配见 [yaml-config.md](yaml-config.md)。

> 核心分层：**Analyzer 只产 `EventFact`（量事实），Judge 消费事实出 `(events, alarms)`（下判断）**。阈值/required 归 Judge，不进 `EventFact.meta`。

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
from app.services.inference.workflows.judge import Judge
from app.services.inference.data_models import (
    AlarmInfo, AlarmType, DetectionOutput, EventFact,
    VisualizationData, VisItem, VisualizationType,
)

logger = logging.getLogger(__name__)


# ====== L1 推理线程：Detector（无状态，多 Client 共享） ======

class XxxDetector(YOLODetector):
    def __init__(self, model_path: str, conf_threshold: float = 0.5,
                 iou_threshold: float = 0.45, enabled: bool = True):
        super().__init__(
            name="xxx",                 # ← 必须与 XxxAnalyzer / XxxJudge.name 一致
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


# ====== L3 时序线程：TemporalAnalyzer（有状态，只产 EventFact） ======

class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, name: str = "xxx"):
        super().__init__(name=name)            # ← 与 Detector / Judge.name 一致
        self._sm = {
            "last_ts": 0.0,        # 游标：已处理到的最新帧 timestamp
            "consecutive": 0,      # 连续命中帧计数（跨 tick 累积，命中 +1，未命中归 0）
        }

    def trans(self, frames: List[DetectionOutput]) -> List[DetectionOutput]:
        return frames                          # 简单任务直接透传；复杂任务在此做特征聚合

    def infer(self, feats: List[DetectionOutput]) -> int:
        # 游标推进：只处理上次 tick 之后的新帧，避免同一帧跨 tick 重复计数（见 SKILL.md §游标）
        last_ts = self._sm["last_ts"]
        new_frames = [f for f in feats if f.timestamp > last_ts]
        for output in new_frames:
            if len(output.detections) > 0:
                self._sm["consecutive"] += 1
            else:
                self._sm["consecutive"] = 0
        if new_frames:
            self._sm["last_ts"] = new_frames[-1].timestamp
        return self._sm["consecutive"]         # 返回测得指标，不判告警

    def post_process(self, raw: int, ts: float) -> List[EventFact]:
        return [EventFact(source=self.name, signal="consecutive", value=raw, ts=ts)]


# ====== L4 时序线程：Judge（有状态，消费事实出告警） ======

class XxxJudge(Judge):
    def __init__(self, consecutive_trigger: int = 3, name: str = "xxx"):
        super().__init__(name=name)            # ← 与 Detector / Analyzer.name 一致
        self.consecutive_trigger = consecutive_trigger   # 阈值归 Judge
        self._sm = {"alarming": False}         # 上升沿锁存

    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        if not facts:
            return [], []
        frame = self._frame(facts)             # {signal: fact} 快照
        f = frame.get("consecutive")
        if f is None:
            return [], []
        consecutive = f.value

        is_triggered = consecutive >= self.consecutive_trigger
        events = [f"xxx in {consecutive} consecutive frames"] if is_triggered else []

        # 实时告警：上升沿触发，下降沿复位
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
    # Analyzer / Judge 同模板 A
```

---

## 模板 C

**结算式告警任务**。实时阶段只产 events、**不报警**，任务结束时一次性裁决。参考 [bending.py](../../../../app/services/inference/workflows/bending.py)。要点：Analyzer 照常产 `EventFact`（如累计 count），**Judge 的 `step()` 只产 events 不产告警，把判定放进 `finalize()`**。

```python
# ── L3 Analyzer：产累计计数事实 ──
class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, name: str = "xxx"):
        super().__init__(name=name)
        self._sm = {"last_ts": 0.0, "action_count": 0}

    def trans(self, frames: List[DetectionOutput]) -> List[DetectionOutput]:
        return frames

    def infer(self, feats: List[DetectionOutput]) -> int:
        # ... 推进游标、按状态机累加 self._sm["action_count"] ...
        return self._sm["action_count"]

    def post_process(self, raw: int, ts: float) -> List[EventFact]:
        return [EventFact(source=self.name, signal="count", value=raw, ts=ts)]


# ── L4 Judge：实时只显进度，结束才裁决 ──
class XxxJudge(Judge):
    def __init__(self, required_actions: int = 4, name: str = "xxx"):
        super().__init__(name=name)
        self.required_actions = required_actions
        self._sm = {"action_count": 0}      # 缓存最新计数供 finalize 用

    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        if not facts:
            return [], []
        cnt = self._frame(facts).get("count")
        if cnt is not None:
            self._sm["action_count"] = cnt.value
        n = self._sm["action_count"]
        events = [f"动作 {n}/{self.required_actions}"] if n > 0 else []
        return events, []                   # 实时阶段不上报告警

    def finalize(self) -> List[AlarmInfo]:
        n = self._sm["action_count"]
        if n < self.required_actions:
            return [AlarmInfo(
                alarm_type=AlarmType.PROCESS_VIOLATION,
                alarm_level="warning",
                alarm_message=f"动作不足：{n}/{self.required_actions}",
                metadata={"metric": self.name, "count": n, "required": self.required_actions},
            )]
        return []
```

> YAML 记得加 `realtime: false`（纯结算告警，不纳入 signals_10s）。

---

## 模板 D

**在线轻量因果序列模型**（analyzer 内嵌轻量 TCN/GRU 等）。

**适用场景**：纯逻辑状态机算不出指标，需要一个**小的因果序列模型**吃一段窗口做前向。模型在 analyzer 的 `infer()` 里直接加载与调用，**不建任何额外基础设施**（详见 SKILL.md §`infer()` 两条路径·路径②）。

**设计要点**：
1. **模型直接进 analyzer**：`__init__` 里 `torch.jit.load(model_path).eval()`（实测 ~5ms，set_task 路径无感），每 client 各一份、不共享、无需锁。**不建 registry / 基类 / 不转 onnx**——2-3 客户端 / 1Hz 下全是过度设计。
2. ⚠️ **滑窗长度 ≥ 模型感受域**：`infer()` 吃的窗口必须够长，否则模型看不到足够历史、恒输出无意义结果。不足时直接不前向。
3. **窗口/特征自管**：与纯逻辑分析器一样用游标推进，特征缓冲在 `self._sm` 自管，按 `window_seconds` 裁剪，绝不调全局 `_slide_window_seconds`。
4. **告警仍归 Judge**：analyzer 把模型输出解码成 `EventFact`，Judge 照常 `step()`/`finalize()`。

```python
import torch

class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, model_path: str, receptive_field: int = 300,
                 window_seconds: float = 10.0, name: str = "xxx"):
        super().__init__(name=name)
        m = torch.jit.load(model_path, map_location="cpu"); m.eval()
        self._model = m
        self.receptive_field = receptive_field      # 模型感受域（帧）
        self.window_seconds = window_seconds        # 指标窗口，自管
        self._sm = {"last_ts": 0.0, "feat_buf": []} # [(ts, feat_vec)] 自管缓冲

    def trans(self, frames: List[DetectionOutput]) -> np.ndarray:
        # 游标推进：只取新帧，追加紧凑特征，按 window_seconds 裁剪（廉价，每 tick 都做）
        last_ts = self._sm["last_ts"]
        for f in (x for x in frames if x.timestamp > last_ts):
            self._sm["feat_buf"].append((f.timestamp, self._to_feat(f)))
            self._sm["last_ts"] = f.timestamp
        cutoff = frames[-1].timestamp - self.window_seconds
        self._sm["feat_buf"] = [(t, v) for t, v in self._sm["feat_buf"] if t >= cutoff]
        return np.asarray([v for _, v in self._sm["feat_buf"]], dtype="float32")  # (T, C)

    def infer(self, feats: np.ndarray):
        if len(feats) < self.receptive_field:       # ⚠️ 窗口不足感受域：不前向
            return None
        with torch.no_grad():
            logits = self._model(torch.from_numpy(feats)[None])   # (1, T, K)
        return logits[0, -1].numpy()                # 取最后一帧的 logits（因果）

    def post_process(self, raw, ts: float) -> List[EventFact]:
        if raw is None:
            return []
        cls = int(raw.argmax())
        return [EventFact(source=self.name, signal="phase", value=cls, ts=ts,
                          conf=float(raw.max()))]

    def _to_feat(self, output: DetectionOutput) -> np.ndarray:
        ...                                         # DetectionOutput → 紧凑特征向量
```

> 配套 Judge 按告警模式选模板 A（实时）或 C（结算）的 Judge 写法。
>
> **越界提示**：重模型全序列分割（MS-TCN 类，感受域 ≈2047 帧 ≫ 在线窗口、需大量未来帧）**不适用本模板**——那是离线链路职责，离线 worker 读 FeatureStore 全序列推理，不在本 skill 在线范围。
