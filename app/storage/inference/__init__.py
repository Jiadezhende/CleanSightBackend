"""inference 域 —— `{step}/inference/` 下推理链路产物的编解码与读写。

    from app.storage import inference

    inference.append_detections(task_id, step_id, [frame, ...])     # 在线写回，追加
    inference.read_detections(task_id, step_id)                     # 离线回读，ts 升序
    inference.detections_stamp(task_id, step_id)                    # 版本戳，核对输入没被追加 / 换代
    facts = inference.read_temporal(task_id, step_id)               # 读 → 合并 → 写
    inference.write_temporal(task_id, step_id, merged)              # 整体替换
    inference.delete(task_id, step_id)                            # 新 run 起始清掉上一代

两份产物按**产出层**分模块：`_detection` 管目标检测产物（L1），`_temporal` 管时序分析产物
（L3）+ 离线逐帧类别概率。共用 `_layout`（域根与文件名）和 `_jsonl`（行框定与原子写）。

## 对外成员

    detections.jsonl      append_detections / read_detections        路线 B（追加）
                          detections_stamp                           版本戳（只做相等比较）
    temporal.jsonl      read_temporal / write_temporal         路线 C（原子整体替换）
    label_probs.npz     read_label_probs / write_label_probs   路线 C（原子整体替换）
    整域                delete                                 三份产物一起没

货币是 `app.domain` 的跨服务契约：`FrameDetection`（`app.domain.detection`）与
`TemporalEvent` / `TemporalSegment` / `LabelProbs`（`app.domain.temporal`）。本域不出自己的类型——没有「从文件名
解出来的身份」这种形状（对照 `hls.SegmentRef`），故没有 `types.py`。

## 两条调用方必须知道的约束

- **`write_temporal` 是整体替换，不是追加。** `temporal.jsonl` 是多写者共居文件，盲写会吃掉别的
  producer 的分段与所有 `TemporalEvent`。正确姿势：`read_temporal` → 丢掉自己这个 producer 的旧条目
  → `write_temporal(合并结果)`。保留谁是 producer 语义，不是格式事实，故留在调用方。
- **迟到的写者不建目录**：谁有权删、谁才有权建。离线这类迟到写者对 `write_temporal` /
  `write_label_probs` 传 `create=False`，域目录已被回收（TTL / 换代）即抛 `DirectoryGoneError`、
  不重建；调用方按「丢弃本次写入」处理。「目录在否」与写入由文件系统原子完成；它管不到对方的 rmtree
  是复合写（写入插在中途会留下半删目录），也管不到同路径被新一代重建（ABA）。
- **`read_detections` 按 ts 升序是契约；`read_temporal` 不排序。** 后者两型没有共同时间键
  （`TemporalEvent.ts` 对 `TemporalSegment.start`），层没有依据替调用方选。

## 刻意没有的成员

    append_temporal     `TemporalEvent` 今天零生产者；在线打点落地时再加，那时它才有 owner
    iter_detections     离线要的是全序列，流式口没有消费方
    detections_path 等  层外没有取用载荷字节的位置，不出路径

## 落盘结构

    {root}/{task_id}/{step_id}/inference/
      detections.jsonl      每帧一行：ts + {流名: [检测框]} + 帧分辨率
      temporal.jsonl      每条一行：`type` 判别 event / segment
      label_probs.npz     离线分割逐帧类别概率：ts [T] + probs [T,C] + labels [C]
      .{name}.tmp         路线 C 的暂存，换名后即消失

## 边界

**不管**：批缓冲、run 生命周期（谁该 supersede、何时 flush）、失败要不要重试、事实该保留
谁——全在调用方。**本域不持锁**：同一 step 的写与 `delete` / `tasks.delete_step` 由调用侧
串行（规范 §6）。IO 失败一律 `OSError` 原样抛，包成什么由调用方定。
"""

from ._detection import append_detections, detections_stamp, read_detections
from ._layout import delete
from ._temporal import read_label_probs, read_temporal, write_label_probs, write_temporal

__all__ = [
    "append_detections",
    "delete",
    "detections_stamp",
    "read_detections",
    "read_label_probs",
    "read_temporal",
    "write_label_probs",
    "write_temporal",
]
