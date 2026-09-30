# 按 ts 降采样上提为推理域共享纯函数

> **变更状态**：生效中（2026-09-27）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-09-30）

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
| ~~网格 `next_t` 浮点累加偶发单帧相位滑动~~ | 判断有误：非偶发，见下方「修订」 | 已修 |
| 在线 `gru-final.pt` 入模帧随容差修复改变（向训练口径靠拢） | 线上时序动作结果会有变化 | 上线后观察在线动作分段 |

## 修订：网格比较加容差 + strict 模式（2026-09-27）

**问题**：整数比下网格点与帧 ts 恰好重合，`ts >= next_t` 是压线比较——ts 6 位小数舍入或真实抖动都会让该帧被跳过。
原测试只断言帧数与平均帧率，没发现间隔错乱。实测（保留帧间隔，以源帧数计）：

```text
场景                        保留帧    修前间隔分布          修后
15→7.5，ts 6 位小数         75/150   3:25  1:25  2:24      2:74
15→7.5，ts 不舍入           75/150   3:7   1:6   2:61      2:74
7.5→7.5，ts 6 位小数        61/72    2:11  1:49            72/72，1:71
```

间隔错乱使 speed（按契约 fps 算）放大 1.5 倍或压一半、窗口实际时长抖动；在线 `gru-final.pt` 与离线 nodep GRU 均受影响。

**改动**：

| 部件 | 改动 |
|------|------|
| `resample_by_ts` | 取保留条件 `ts >= next_t - tol`、重锚条件 `next_t - tol <= ts`；`tol` = 半个输入帧间隔（相邻正 ts 差的中位数） |
| `resample_by_ts(..., strict=)` | 默认宽松（输入慢于 `fps` 原样放行，在线沿用）；`strict=True` 时输入帧率低于 `fps`（超 1% 余量）抛 `ValueError` |
| `CleanNodepGRUSegmenter.preprocess` | 调用改为 `strict=True` |
| `tests/test_inference_resample.py` | 2:1 断言间隔全为 2（6 位小数 ts / 抖动 ts）、1:1 全保留、strict 拒慢输入、strict 接受等帧率 |

**自测**：`tests/test_inference_resample.py` 9 passed；全量 `pytest tests/` 815 passed, 8 skipped。
