"""时序分析事实契约（L3 产出）。

两类事实按时间粒度分型，同落 `facts.jsonl`（一条一行）：

    EventFact    点  —— 某信号在某一帧上的电平
    SegmentFact  区间 —— 一段时间里的一个动作 / 状态
    Fact         两者的并，读写两侧的货币

三条硬约束：

- **时间轴**：`ts` / `start` / `end` 均为**帧捕获墙钟 ts**（epoch 秒），与 `FrameFeature.ts`、
  HLS sidecar `.idx` 的逐帧数组同源同值——事实靠这条与录像互相定位。
- **`producer` 是产出者身份的唯一真源**：幂等替换按它过滤，别再往 `meta` 里盖第二份。
- **`meta` 只放伴随观测量**：任何被代码读来做判断的键都不许进去。

身份键 `(task_id, step_id)` 不在本模块——它由落盘路径携带。落盘的 `type` 判别字段同样不在
这里：那是格式知识，归 `app/storage/` 的 codec。

> ⚠ `app/services/inference/types.py` 里还有一对同名旧型（字段是 `source`，事实身份塞在
> `meta["producer"]`），调用点分批迁移中。字段已改名，两边混用是 `TypeError`，不会静默。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Union


@dataclass
class EventFact:
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
class SegmentFact:
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


# 读写两侧的货币：`read_facts` 出它的列表，`write_facts` 收它的序列。
Fact = Union[EventFact, SegmentFact]
