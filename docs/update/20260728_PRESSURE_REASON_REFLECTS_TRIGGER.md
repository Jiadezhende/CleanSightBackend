# [PRESSURE] 的 reason 如实反映触发侧（新增 counter_growth）

> **变更状态**：生效中（2026-07-28）
> **知识库**：已沉淀 → [DESIGN_OBSERVABILITY](../kb/DESIGN_OBSERVABILITY.md)（reason 触发侧语义）(2026-08-02)

## 概述

- **改了什么**：`PressureReporter` 新增 `REASON_COUNTER_GROWTH`；谓词为假、仅因 `*_total` 增长而打的行，reason 改挂 `counter_growth`，不再原样打调用方传入的水位 reason。
- **为什么改**：现状会打出自相矛盾的行。
- **影响面**：仅 `app/utils/pressure.py`（`_observe` 一处 + 常量/注释）。调用方（`ClientQueues`、`StageAwareDispatcher`）**不改**——它们照旧传自己的谓词 reason，由 reporter 决定是否采用。

## 问题：谓词没响，却报了水位

压力判定是 `pressured or growing`（谓词 OR 任一累计计数在涨），这是有意设计——「丢完就空、水位天然测不到」，只有 delta 能报出来。但 `reason` 一直原样打调用方传入的常量，与实际触发侧无关。于是排查 issue #82 时打出这种行：

```
[PRESSURE] component=dispatcher resource=stage_queue stage=2
  depth=0 capacity=256 utilization=0.000
  drop_total=0 drop_delta=0
  reject_total=351 reject_delta=160 reason=queue_high_watermark
```

`utilization=0.000` 与 `reason=queue_high_watermark` 直接打架。真实情况是队列一帧没积压，只是 `reject_total` 在涨（下游 inflight 偶尔打满，属稳态正常），但 reason 让人第一眼读成「队列在高水位积压」。

## 改法

```python
# reason 如实反映触发侧：谓词没响就不能挂调用方的水位 reason。
# 两者同时成立时以谓词为准——水位是更强的信号，计数增长由行内 *_delta 自明。
line = self._format(reason if pressured else REASON_COUNTER_GROWTH, fields, deltas)
```

同一行修好后长这样：

```
... depth=0 utilization=0.000 reject_total=351 reject_delta=160 reason=counter_growth
```

## 边界：reason 的轴是「触发侧」，不是「成因」

模块原注释写着「别为每种成因再造一个 reason」，这条仍然成立、且**没有**被本次改动破坏——两者是不同的轴：

- **触发侧**（本次新增的区分）：这一行是因为水位越线打的，还是因为计数还在涨打的。只有两个值，低基数。
- **成因**（仍由行内字段区分）：同样是队列积压，`reject_delta>0` 说明下游在拒收，否则是取帧快于提交。这一轴**不进 reason**。

## 测试

`tests/test_pressure_reporter.py`：
- `test_growing_counter_alone_is_pressure` 扩断言 `reason=counter_growth` 且不含 `queue_high_watermark`。
- 新增 `test_predicate_wins_reason_when_both_fire`：两者同时成立时取调用方 reason。

`tests/test_cq_pressure_log.py` 不变——它断言的那条确实越了水位（`utilization=0.500`），reason 仍是 `queue_high_watermark`。
