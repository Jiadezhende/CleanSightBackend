# 按 ts 降采样上提为推理域共享纯函数

> **变更状态**：生效中（2026-09-27）
> **知识库**：待沉淀

## 概述

在线 `TemporalOperator._resample_by_ts` 的相位网格抽稀算法原样上提为 [`resample_by_ts(frames, fps)`](../../app/services/inference/resample.py)，
在线方法改为一行委托，行为不变。为离线 Segmenter 按训练帧率降采样复用做准备（离线不得 import 在线模块）。

## 变更背景

- **现状**：按 ts 降采样只存在于在线 Operator 的实例方法里，读 `self.model_input_fps`；离线接入新 GRU 模型（16 帧窗口按帧计数，须喂训练帧率）也需要同一算法。
- **约束**：离线链路不拉在线服务模块，复制一份又违背单一实现。故放到 online / offline 共用的 `app/services/inference/` 顶层（与 `config.py`、`stage_factory.py` 同级）。

## 方案详情

| 部件 | 改动 |
|------|------|
| `app/services/inference/resample.py`（新） | `resample_by_ts(frames, fps)`：算法与原方法逐行一致；`len < 2` 时返回列表副本（原为同一对象，调用方不依赖身份） |
| `app/services/inference/online/temporal/operator.py` | `_resample_by_ts` 改为 `return resample_by_ts(frames, self.model_input_fps)` |
| `tests/test_inference_resample.py`（新） | 15→7.5fps 帧数与平均帧率、缺口重锚、目标帧率高于输入时全保留、`<2` 帧、不复制帧对象 |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_inference_resample.py` | 5 passed |
| 全量 `pytest tests/` | 951 passed, 8 skipped |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 网格 `next_t` 浮点累加偶发单帧相位滑动（原算法即有：某处间隔 3/15 紧跟 1/15） | 帧数与平均帧率不漂，局部间隔抖一帧；真实 ts 本身带抖动，影响可忽略 | 暂不处理 |
