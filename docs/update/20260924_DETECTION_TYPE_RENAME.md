# 检测契约三型改名：一个粒度一个名词

> **变更状态**：生效中（2026-09-24）　<!-- 纯改名，零行为变更 -->
> **知识库**：待沉淀

## 概述

`app/domain/detection.py` 三个类改名：`Detection` → `DetBox`、`FrameDetections` → `DetectorOutput`、`FrameFeature` → `FrameDetection`。代码、注释、`DEVELOPMENT.md` 与 `infer-workflow` / `temporal-review` 两个 skill 同步；字段、落盘格式、行为不变。

## 变更背景

- **现状 / 痛点**：三个粒度的名字都带 detection，只靠前缀区分，嵌套访问时分不清层级。`FrameFeature` 装的是对齐后的检测，不是算出来的特征（docstring 自认），与算子里真正的 `features` 张量撞词。它是检测层（L1）产出，与时序层（L3）的 `Fact` 同级。
- **承接**：[20260906_OFFLINE_DATA_MODEL_NAMING.md](20260906_OFFLINE_DATA_MODEL_NAMING.md) 的 B 档提案（当时建议 `AlignedFrame` / `FrameRecord`，未落地）；本批取代其类名建议。

## 方案详情

### 全景：三个粒度

```text
DetBox           一个框                          bbox / confidence / class_id / class_name
  └ DetectorOutput 一个检测器 × 一帧               detections / metadata / timestamp / success / error
      └ FrameDetection 所有检测器 × 一帧（多流对齐）  ts / by_source{流名: DetectorOutput} / frame_width / frame_height
```

| 旧 | 新 |
|----|----|
| `Detection` | `DetBox` |
| `FrameDetections` | `DetectorOutput` |
| `FrameFeature` | `FrameDetection` |

### 保留项

- `docs/update/` 历史记录与 `docs/kb/` 不回溯改写（KB 走融合流程）。
- `FrameInference` 合并、字段瘦身、`features` → `detections` 链路改名在后续批次，见下。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 866 passed，未改任何断言值 |
| 旧名残留 | `app/` `tests/` `integration_tests/` `scripts/` 按 ASCII 标识符边界 grep 为 0 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `FrameDetection` 与旧名 `FrameDetections` 只差一个 s，但粒度不同 | 读历史文档 / KB 时易误认 | KB 融合时统一改写；口头交流用新名 |
| `FrameInference` 与 `FrameDetection` 同一份数据两种形态 | 名字仍不统一 | 下一批合并 |
