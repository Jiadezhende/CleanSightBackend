"""inference 域 —— `{step}/inference/` 下推理链路产物的编解码与读写。

    from app.storage import inference

    inference.append_features(task_id, step_id, [feature, ...])   # 在线写回，追加
    inference.read_features(task_id, step_id)                     # 离线回读，ts 升序
    facts = inference.read_facts(task_id, step_id)                # 读 → 合并 → 写
    inference.write_facts(task_id, step_id, merged)               # 整体替换
    inference.delete(task_id, step_id)                            # 新 run 起始清掉上一代

两份产物按**产出层**分模块：`_detection` 管目标检测产物（L1），`_temporal` 管时序分析产物
（L3）+ 离线调试件。共用 `_layout`（域根与文件名）和 `_jsonl`（行框定与原子写）。

## 对外成员

    features.jsonl      append_features / read_features        路线 B（追加）
    facts.jsonl         read_facts / write_facts               路线 C（原子整体替换）
    offline_debug.json  write_debug_result                     路线 C
    整域                delete                                 三份产物一起没

货币是 `app.domain` 的跨服务契约：`FrameDetection`（`app.domain.detection`）与
`Fact = EventFact | SegmentFact`（`app.domain.fact`）。本域不出自己的类型——没有「从文件名
解出来的身份」这种形状（对照 `hls.SegmentRef`），故没有 `types.py`。

## 两条调用方必须知道的约束

- **`write_facts` 是整体替换，不是追加。** `facts.jsonl` 是多写者共居文件，盲写会吃掉别的
  producer 的分段与所有 `EventFact`。正确姿势：`read_facts` → 丢掉自己这个 producer 的旧条目
  → `write_facts(合并结果)`。保留谁是 producer 语义，不是格式事实，故留在调用方。
- **`read_features` 按 ts 升序是契约；`read_facts` 不排序。** 后者两型没有共同时间键
  （`EventFact.ts` 对 `SegmentFact.start`），层没有依据替调用方选。

## 刻意没有的成员

    append_facts        `EventFact` 今天零生产者；在线打点落地时再加，那时它才有 owner
    iter_features       离线要的是全序列，流式口没有消费方
    features_path 等    层外没有取用载荷字节的位置，不出路径
    read_debug_result   调试产物是给人看的

## 落盘结构

    {root}/{task_id}/{step_id}/inference/
      features.jsonl      每帧一行：ts + {流名: [检测框]} + 帧分辨率
      facts.jsonl         每条一行：`type` 判别 event / segment
      offline_debug.json  离线策略逐帧中间量，无固定形状
      .{name}.tmp         路线 C 的暂存，换名后即消失

## 边界

**不管**：批缓冲、run 生命周期（谁该 supersede、何时 flush）、失败要不要重试、事实该保留
谁——全在调用方。**本域不持锁**：同一 step 的写与 `delete` / `tasks.delete_step` 由调用侧
串行（规范 §6）。IO 失败一律 `OSError` 原样抛，包成什么由调用方定。
"""

from ._detection import append_features, read_features
from ._layout import delete
from ._temporal import read_facts, write_debug_result, write_facts

__all__ = [
    "append_features",
    "delete",
    "read_facts",
    "read_features",
    "write_debug_result",
    "write_facts",
]
