"""时序分析产出契约（L3）。

两类事实按时间粒度分型，同落 `temporal.jsonl`（一条一行）：

    TemporalEvent    点  —— 某信号在某一帧上的电平
    TemporalSegment  区间 —— 一段时间里的一个动作 / 状态

另有一份非事实的旁路产物，落 `label_probs.npz`：

    LabelProbs       逐帧类别概率 —— 离线分割模型的原始输出，仅供可视化，不参与任何判断

三条硬约束：

- **时间轴**：`ts` / `start` / `end` 均为**帧捕获墙钟 ts**（epoch 秒），与 `FrameDetection.ts`、
  HLS sidecar `.idx` 的逐帧数组同源同值——事实靠这条与录像互相定位。
- **`producer` 是产出者身份的唯一真源**：幂等替换按它过滤，别再往 `meta` 里盖第二份。
- **`meta` 只放伴随观测量**：任何被代码读来做判断的键都不许进去。

身份键 `(task_id, step_id)` 不在本模块——它由落盘路径携带。落盘的 `type` 判别字段同样不在
这里：那是格式知识，归 `app/storage/inference` 的 codec。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

import numpy as np


@dataclass
class TemporalEvent:
    """打点：某信号在某一帧上的电平。

    多信号靠不同 `signal` 名区分，不是类型枚举；同一算子一个 tick 可产多条。
    """

    producer: str  # 产出者 = Operator.name
    signal: str  # 信号名，如 "birth_rate" / "state" / "count"
    value: Any  # 该信号在 ts 的取值
    ts: float  # 帧捕获 ts —— 必填、无默认：默认取墙钟会与录像差一个推理延迟
    conf: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TemporalSegment:
    """分段：一段时间里的一个动作 / 状态。

    闭区间 `[start, end]`，单帧段 `start == end`。合法性（有限数、start <= end、conf 值域）
    由产出侧校验，本类不自检。
    """

    producer: str  # 产出者 = OfflineSegmenter.name
    label: str  # 动作标签，如 "long_brush_insert"
    start: float  # 帧捕获 ts，区间左端
    end: float  # 帧捕获 ts，区间右端（>= start）
    conf: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, eq=False)
class LabelProbs:
    """逐帧类别概率：离线分割模型在每帧上对各 label 的 softmax 输出。

    `ts[i]` 与 `probs[i]` 同行；`labels[j]` 是 `probs[:, j]` 的类名，含背景类（如 `idle`）。
    形状一致性（`len(ts) == probs.shape[0]`、`len(labels) == probs.shape[1]`）由产出侧校验，
    本类不自检。`eq=False`：ndarray 字段不支持逐值 `==`。
    """

    ts: np.ndarray  # [T] float64，帧捕获 ts，与 detections.jsonl 位级相等
    probs: np.ndarray  # [T, C] float，行为该帧的类别分布
    labels: Tuple[str, ...]  # C 个类名，顺序即 probs 的列序
