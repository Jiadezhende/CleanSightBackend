# 代码模板

四种形态各一份骨架，每份含 **Detector(L1) + Analyzer(L3) + Judge(L4)** 三件套。三个类 `name` 必须一致；签名照抄基类。

- [A — YOLO + 实时告警（最常见）](#模板-a) ｜ [B — 无模型纯算法](#模板-b) ｜ [C — 结算告警](#模板-c) ｜ [D — 在线轻量序列模型](#模板-d)

**Analyzer 只产 `EventFact`，Judge 出 `(events, alarms)`**。字段见 [data-models.md](data-models.md)，装配见 [yaml-config.md](yaml-config.md)。

---

## 模板 A

YOLO + 实时告警。参考 [bubble.py](../../../../app/services/inference/workflows/bubble.py)。

```python
import logging, time
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


class XxxDetector(YOLODetector):
    def __init__(self, model_path: str, conf_threshold: float = 0.5,
                 iou_threshold: float = 0.45, enabled: bool = True):
        super().__init__(name="xxx", model_path=model_path,
                         conf_threshold=conf_threshold, iou_threshold=iou_threshold,
                         enabled=enabled)

    def infer_batch(self, frames: List[np.ndarray],
                    contexts: List[Dict[str, Any]]) -> List[DetectionOutput]:
        try:
            outputs = self._run_yolo_batch(frames)
            for o in outputs:
                o.success = True
                o.xxx_detected = len(o.detections) > 0       # 业务字段
            return outputs
        except Exception as e:
            logger.error("[XxxDetector] batch failed, fallback: %s", e, exc_info=True)
            results = []
            for f, c in zip(frames, contexts):
                try:
                    o = self.infer(f, c)
                    o.xxx_detected = len(o.detections) > 0    # ⚠️ 与 batch 路径逻辑一致
                    results.append(o)
                except Exception as err:
                    results.append(DetectionOutput(detections=[], metadata={"error": str(err)},
                                                   timestamp=time.time(), success=False, error=str(err)))
            return results

    def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
        items = [VisItem(bbox=d.bbox, label=f"{d.class_name} {d.confidence:.2f}",
                         confidence=d.confidence, color=(0, 0, 255))      # BGR
                 for d in output.detections]
        detected = len(output.detections) > 0
        return VisualizationData(type=VisualizationType.BBOX, items=items,
                                 status_text="Detected!" if detected else "Normal",
                                 status_color=(0, 0, 255) if detected else (0, 255, 0),
                                 status_position="top-left")


class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, name: str = "xxx"):
        super().__init__(name=name)
        self._sm = {"last_ts": 0.0, "consecutive": 0}

    def trans(self, frames: List[DetectionOutput]) -> List[DetectionOutput]:
        return frames

    def infer(self, feats: List[DetectionOutput]) -> int:
        last_ts = self._sm["last_ts"]                          # 游标推进
        new = [f for f in feats if f.timestamp > last_ts]
        for o in new:
            self._sm["consecutive"] = self._sm["consecutive"] + 1 if o.detections else 0
        if new:
            self._sm["last_ts"] = new[-1].timestamp
        return self._sm["consecutive"]

    def post_process(self, raw: int, ts: float) -> List[EventFact]:
        return [EventFact(source=self.name, signal="consecutive", value=raw, ts=ts)]


class XxxJudge(Judge):
    def __init__(self, consecutive_trigger: int = 3, name: str = "xxx"):
        super().__init__(name=name)
        self.consecutive_trigger = consecutive_trigger
        self._sm = {"alarming": False}

    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        f = self._frame(facts).get("consecutive")
        if f is None:
            return [], []
        n = f.value
        triggered = n >= self.consecutive_trigger
        events = [f"xxx in {n} consecutive frames"] if triggered else []
        alarms: List[AlarmInfo] = []
        if triggered and not self._sm["alarming"]:
            self._sm["alarming"] = True
            alarms.append(AlarmInfo(alarm_type=AlarmType.PROCESS_VIOLATION, alarm_level="high",
                                    alarm_message="检测到 xxx 异常",
                                    metadata={"consecutive_frames": n}))
        elif not triggered and self._sm["alarming"]:
            self._sm["alarming"] = False
        return events, alarms
```

> 复杂指标（ByteTrack + birth_rate）把游标推进/算指标拆成 `_advance`/`_compute_metric`，见 [bubble.py](../../../../app/services/inference/workflows/bubble.py)。

---

## 模板 B

无模型纯算法：Detector 继承 `Detector` 实现 `infer`，其余同模板 A。参考 [mock.py](../../../../app/services/inference/workflows/mock.py)。

```python
from app.services.inference.workflows.detector import Detector

class XxxDetector(Detector):
    def __init__(self, enabled: bool = True):
        super().__init__(name="xxx", enabled=enabled)

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        return DetectionOutput(detections=[...], metadata={"model": "xxx_algo"},
                               timestamp=time.time(), success=True)
    # infer_batch 默认逐帧；prepare_visualization_data 同 A；Analyzer/Judge 同 A
```

---

## 模板 C

结算告警：实时只产 events，结束才裁决。参考 [bending.py](../../../../app/services/inference/workflows/bending.py)。Analyzer 照常产 EventFact（累计 count）；Judge 的 `step()` 只产 events，判定放 `finalize()`。

```python
class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, name: str = "xxx"):
        super().__init__(name=name)
        self._sm = {"last_ts": 0.0, "action_count": 0}

    def trans(self, frames): return frames
    def infer(self, feats) -> int:
        ...                                          # 推游标，累加 self._sm["action_count"]
        return self._sm["action_count"]
    def post_process(self, raw: int, ts: float) -> List[EventFact]:
        return [EventFact(source=self.name, signal="count", value=raw, ts=ts)]


class XxxJudge(Judge):
    def __init__(self, required_actions: int = 4, name: str = "xxx"):
        super().__init__(name=name)
        self.required_actions = required_actions
        self._sm = {"action_count": 0}

    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        cnt = self._frame(facts).get("count")
        if cnt is not None:
            self._sm["action_count"] = cnt.value
        n = self._sm["action_count"]
        return ([f"动作 {n}/{self.required_actions}"] if n > 0 else []), []   # 实时不报警

    def finalize(self) -> List[AlarmInfo]:
        n = self._sm["action_count"]
        if n < self.required_actions:
            return [AlarmInfo(alarm_type=AlarmType.PROCESS_VIOLATION, alarm_level="warning",
                              alarm_message=f"动作不足：{n}/{self.required_actions}",
                              metadata={"metric": self.name, "count": n, "required": self.required_actions})]
        return []
```
> YAML 加 `realtime: false`。

---

## 模板 D

在线轻量因果序列模型：analyzer 内嵌轻量 TCN/GRU。规则：模型进 analyzer `__init__`（每实例自加载，**不建 registry/基类、不转 onnx**）；⚠️ **窗口 ≥ 感受域**，不足不前向；特征/窗口用游标自管。配套 Judge 按实时(A)或结算(C)选。

```python
import torch

class XxxAnalyzer(TemporalAnalyzer):
    def __init__(self, model_path: str, receptive_field: int = 300,
                 window_seconds: float = 10.0, name: str = "xxx"):
        super().__init__(name=name)
        m = torch.jit.load(model_path, map_location="cpu"); m.eval()
        self._model = m
        self.receptive_field = receptive_field
        self.window_seconds = window_seconds
        self._sm = {"last_ts": 0.0, "feat_buf": []}           # [(ts, feat_vec)] 自管

    def trans(self, frames: List[DetectionOutput]) -> np.ndarray:
        last_ts = self._sm["last_ts"]
        for f in (x for x in frames if x.timestamp > last_ts):
            self._sm["feat_buf"].append((f.timestamp, self._to_feat(f)))
            self._sm["last_ts"] = f.timestamp
        cutoff = frames[-1].timestamp - self.window_seconds
        self._sm["feat_buf"] = [(t, v) for t, v in self._sm["feat_buf"] if t >= cutoff]
        return np.asarray([v for _, v in self._sm["feat_buf"]], dtype="float32")   # (T, C)

    def infer(self, feats: np.ndarray):
        if len(feats) < self.receptive_field:                 # ⚠️ 窗口不足感受域
            return None
        with torch.no_grad():
            return self._model(torch.from_numpy(feats)[None])[0, -1].numpy()   # 取最后一帧(因果)

    def post_process(self, raw, ts: float) -> List[EventFact]:
        if raw is None:
            return []
        return [EventFact(source=self.name, signal="phase", value=int(raw.argmax()),
                          ts=ts, conf=float(raw.max()))]

    def _to_feat(self, output: DetectionOutput) -> np.ndarray:
        ...                                                   # DetectionOutput → 紧凑特征向量
```
> 重模型全序列分割（MS-TCN，感受域 ≈2047 帧、需大量未来帧）走**离线链路**读 FeatureStore，不用本模板。
