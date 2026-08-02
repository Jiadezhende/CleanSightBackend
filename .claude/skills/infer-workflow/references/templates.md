# 代码模板

四种形态各一份骨架，每份含 **流源 Detector + 流算子 Operator** 两件套。**落点：一文件一基类**——`XxxDetector` 写 `app/services/inference/detection/impl/<业务>.py`，`XxxOperator` 写 `app/services/inference/temporal/impl/<业务>.py`（下方代码块把两者并排只为便于对照，落地时拆两个同名文件；别塞进同一文件）。`detector.name` = 产出流名，写进算子 `subscribes`；算子 `name` 是自身身份，可不同。签名照抄基类。字段见 [data-models.md](data-models.md)，装配见 [yaml-config.md](yaml-config.md)。

- [A — YOLO + 实时告警（最常见）](#模板-a) ｜ [B — 无模型纯算法](#模板-b) ｜ [C — 结算告警](#模板-c) ｜ [D — 内嵌因果序列模型](#模板-d)

**Operator**：`analyze(windows)` 推 `self._sm`（不返回）；`judge()` 读 `_sm` 出 `(events, alarms)`；可选 `finalize()` 结算。

---

## 模板 A

YOLO + 实时告警。参考 [detection/impl/bubble.py](../../../../app/services/inference/detection/impl/bubble.py)（Detector）+ [temporal/impl/bubble.py](../../../../app/services/inference/temporal/impl/bubble.py)（Operator）。

```python
import logging
from typing import Dict, List, Tuple

from app.services.inference.detection.detector import YOLODetector
from app.services.inference.temporal.operator import Operator
from app.domain.alarm import Alarm, AlarmMetric, AlarmType
from app.domain.detection import FrameDetections
from app.domain.render import RenderItem, RenderSpec, RenderType

logger = logging.getLogger(__name__)


class XxxDetector(YOLODetector):
    def __init__(self, model_path: str, conf_threshold: float = 0.5,
                 iou_threshold: float = 0.45, enabled: bool = True):
        super().__init__(name="xxx", model_path=model_path, conf_threshold=conf_threshold,
                         iou_threshold=iou_threshold, enabled=enabled)

    # YOLODetector.infer_batch 默认已够用；仅当要写业务字段/自定义输出才 override，
    # 且 batch 与 except 逐帧 fallback 的赋值逻辑须一致，timestamps[i] 原样写入 FrameDetections。

    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        items = [RenderItem(bbox=d.bbox, label=f"{d.class_name} {d.confidence:.2f}",
                            confidence=d.confidence, color=(0, 0, 255))       # BGR
                 for d in output.detections]
        detected = len(output.detections) > 0
        return RenderSpec(type=RenderType.BBOX, items=items,
                          status_text="Detected!" if detected else "Normal",
                          status_color=(0, 0, 255) if detected else (0, 255, 0),
                          status_position="top-left")


class XxxOperator(Operator):
    def __init__(self, name: str = "xxx", subscribes: List[str] = None,
                 window_seconds: float = 10.0, consecutive_trigger: int = 3):
        super().__init__(name=name, subscribes=subscribes or ["xxx"], window_seconds=window_seconds)
        self.consecutive_trigger = consecutive_trigger
        self._sm = {"last_ts": 0.0, "consecutive": 0, "alarming": False}

    def analyze(self, windows: Dict[str, List[FrameDetections]]) -> None:
        window = self.primary_window(windows)                 # 单订阅：裁到感受野
        if not window:
            return
        last_ts = self._sm["last_ts"]                         # 游标推进
        new_frames = [f for f in window if f.timestamp > last_ts]
        for f in new_frames:
            self._sm["consecutive"] = self._sm["consecutive"] + 1 if f.detections else 0
        if new_frames:
            self._sm["last_ts"] = new_frames[-1].timestamp

    def judge(self) -> Tuple[List[str], List[Alarm]]:
        n = self._sm["consecutive"]
        triggered = n >= self.consecutive_trigger
        events = [f"xxx in {n} consecutive frames"] if triggered else []
        alarms: List[Alarm] = []
        if triggered and not self._sm["alarming"]:            # 上升沿锁存
            self._sm["alarming"] = True
            alarms.append(Alarm(alarm_type=AlarmType.PROCESS_VIOLATION, alarm_level="high",
                                alarm_message="检测到 xxx 异常", metric=AlarmMetric.XXX,
                                metadata={"consecutive_frames": n}))
        elif not triggered and self._sm["alarming"]:
            self._sm["alarming"] = False
        return events, alarms
```

> 复杂指标（ByteTrack + birth_rate）把游标推进/算指标拆成 `_advance`/`_compute_metric`，派生 history 在 `_sm` 里按 `window_seconds` 自裁，见 [temporal/impl/bubble.py](../../../../app/services/inference/temporal/impl/bubble.py)。`AlarmMetric.XXX` 需先在 [alarm.py](../../../../app/domain/alarm.py) 枚举补一项。

---

## 模板 B

无模型纯算法：Detector 继承 `Detector`，实现 `infer_batch(frames, timestamps)`（无 YOLO）。Operator 同模板 A。参考 [detection/impl/mock.py](../../../../app/services/inference/detection/impl/mock.py)（Detector）+ [temporal/impl/mock.py](../../../../app/services/inference/temporal/impl/mock.py)（Operator）。

```python
import numpy as np
from app.services.inference.detection.detector import Detector
from app.domain.detection import Detection, FrameDetections

class XxxDetector(Detector):
    def __init__(self, enabled: bool = True):
        super().__init__(name="xxx", enabled=enabled)

    def infer_batch(self, frames: List[np.ndarray],
                    timestamps: List[float]) -> List[FrameDetections]:
        out = []
        for frame, ts in zip(frames, timestamps):            # timestamps[i] 原样写入，别自造
            out.append(FrameDetections(detections=[Detection(...)],
                                       metadata={"model": "xxx_algo"},
                                       timestamp=ts, success=True))
        return out
    # prepare_visualization_data 同 A；Operator 同 A
```

---

## 模板 C

结算告警：实时只产 events，结束才裁决。参考 [temporal/impl/bending.py](../../../../app/services/inference/temporal/impl/bending.py)（Operator）+ [detection/impl/bending.py](../../../../app/services/inference/detection/impl/bending.py)（Detector）。analyze 照常推游标累计计数，`judge()` 只产进度 events、`finalize()` 出告警。

```python
class XxxOperator(Operator):
    def __init__(self, name: str = "xxx", subscribes: List[str] = None,
                 window_seconds: float = 10.0, required_actions: int = 4):
        super().__init__(name=name, subscribes=subscribes or ["xxx"], window_seconds=window_seconds)
        self.required_actions = required_actions
        self._sm = {"last_ts": 0.0, "action_count": 0}

    def analyze(self, windows: Dict[str, List[FrameDetections]]) -> None:
        window = self.primary_window(windows)
        if not window:
            return
        ...                                                   # 推游标，累加 self._sm["action_count"]

    def judge(self) -> Tuple[List[str], List[Alarm]]:
        n = self._sm["action_count"]
        return ([f"动作 {n}/{self.required_actions}"] if n > 0 else []), []   # 实时不报警

    def finalize(self) -> List[Alarm]:
        n = self._sm["action_count"]
        if n < self.required_actions:
            return [Alarm(alarm_type=AlarmType.PROCESS_VIOLATION, alarm_level="warning",
                          alarm_message=f"动作不足：{n}/{self.required_actions}",
                          metric=AlarmMetric.XXX,
                          metadata={"count": n, "required": self.required_actions})]
        return []
```
> YAML 加 `realtime: false`。

---

## 模板 D

内嵌因果序列模型：Operator 继承 `GRUOperator`（基类惰性加载 `GRUClassifier`、给 `infer(features)→List[int]`）。子类只写 `_adapt_to_features` + analyze/judge。参考 [temporal/impl/clean.py](../../../../app/services/inference/temporal/impl/clean.py)（Operator）+ [detection/impl/clean.py](../../../../app/services/inference/detection/impl/clean.py)（Detector）。

规则：**模型必须因果**（单向 GRU / causal mask，需未来帧的 MS-TCN 类走离线链路）；⚠️ **窗口帧数 ≥ 感受域**，不足加 warm-up guard（`min_frames`）不前向；多订阅用 `_zip_by_ts` 对齐。**接入 review 与上线门禁（延迟/感受域/参数量）走 `/temporal-review`。**

```python
import torch
from app.services.inference.temporal.operator import AlignedFrame, GRUOperator

class XxxOperator(GRUOperator):
    def __init__(self, name: str, subscribes: List[str], window_seconds: float,
                 model_path: str, objects: Dict[int, str], actions: Dict[int, str],
                 hidden: int = 128, num_layers: int = 3, min_frames: int = 0):
        super().__init__(name=name, subscribes=subscribes, window_seconds=window_seconds,
                         model_path=model_path, objects=objects, actions=actions,
                         hidden=hidden, num_layers=num_layers)
        self.min_frames = min_frames if min_frames > 0 else int(window_seconds)
        self._sm = {"last_ts": 0.0, "latest_action": None}

    def analyze(self, windows: Dict[str, List[FrameDetections]]) -> None:
        aligned = self._zip_by_ts(windows)                    # 多流按 ts 对齐
        if len(aligned) < self.min_frames:                    # ⚠️ warm-up：帧数不足不前向
            return
        if aligned[-1].ts <= self._sm["last_ts"]:             # 游标：无新帧跳过重复推理
            return
        features = self._adapt_to_features(aligned)           # (T, num_objects*4)
        if features.numel() == 0:
            return
        predictions = self.infer(features)                    # 逐时间步类别
        self._sm["latest_action"] = predictions[-1]           # 取尾元素 = 最新动作（因果）
        self._sm["last_ts"] = aligned[-1].ts

    def judge(self) -> Tuple[List[str], List[Alarm]]:
        aid = self._sm["latest_action"]
        events = [f"Action: {self.get_action_name(aid)}"] if aid is not None else []
        return events, []                                     # 本例实时只 overlay 不告警

    def _adapt_to_features(self, aligned_frames: List[AlignedFrame]) -> "torch.Tensor":
        ...   # 每 AlignedFrame → 一行特征；class_id 经 self.objects 映射到全局槽位，别赌两 .pt 共享标签
```

> ⚠️ `_adapt_to_features` 是静默错重灾区（class_id 撞槽、缺席 vs 零框、归一化分辨率来源）——写完务必走 `/temporal-review`。重模型全序列分割（MS-TCN，感受域 ≈2047 帧、需大量未来帧）走**离线链路**读 FeatureStore，不用本模板。
