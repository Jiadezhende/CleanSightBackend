# 离线分析统一推理入口与作业编排

> **变更状态**：生效中（2026-11-24，当前分支）
>
> **知识库**：待沉淀

## 概述

`OfflineSegmenter.infer()` 统一帧准备和推理步骤。`OfflineRunner` 编排同一个 run 的分割、落盘和规则判定，CLI 负责命令行适配和子进程初始化。

## 变更背景

原流程由 Runner 调用多个策略方法、从策略实例取逐帧概率，再由 CLI 接续规则判定。两套 DINOv2 策略各自执行相同的帧准备。统一入口后，一次推理的产物直接返回，离线作业的执行顺序集中在 Runner。

## 方案详情

### 全景

```text
JobService → CLI → Runner.run()
  → 读取检测帧 → infer() 准备帧、预测、校验和解码
  → 写逐帧概率 → 替换动作段 → 判定同一个 run
```

| 步骤 | 模块 | 详情 |
|---|---|---|
| 帧准备与推理 | `offline/segmentation` | 1. 策略推理 |
| 结果提交与规则判定 | `offline/runner.py` | 2. 离线作业 |
| 进程隔离与命令输出 | `offline/cli.py` | 3. CLI |

### 1. 策略单次推理返回动作段和原始概率

- 基类按策略契约依次执行截断、帧率检查与抽稀、去重及尺寸校验。选中帧保留原始时间戳，策略用它精确匹配 raw。 [帧准备](../../app/services/inference/offline/segmentation/utils.py)
- 七个策略分别实现 `_build_model_input()`、`_predict()` 和 `_postprocess()`。策略类路径、权重、特征列和解码参数沿用现有配置。 [基类](../../app/services/inference/offline/segmentation/segmenter.py) · [策略实现](../../app/services/inference/offline/segmentation/impl/)
- `_predict()` 直接返回 `LabelProbs`，同一份数据交给 `_postprocess()` 解码。`infer()` 返回 `(segments, label_probs)`，由 Runner 直接解包。逐帧概率用于 bad case 分析和规则判定，仍以 `float16` 写入 `label_probs.npz`。 [概率存储](../../app/storage/inference/_temporal.py)
- `infer()` 在解码前校验逐帧概率形状，在返回前校验动作段。校验失败抛出 `ValueError`，本次结果不进入提交。 [输出校验](../../app/services/inference/offline/segmentation/utils.py)

### 2. Runner 提交分割后核对同一个 run

- `offline/runner.py` 使用同一份 stage 配置和同一个 `RunIdentity` 写分割结果、执行规则判定。`check()` 复用判定逻辑，支持独立运行和 dry run。 [Runner](../../app/services/inference/offline/runner.py)
- Runner 排序后替换当前 run 的旧动作段，保留已有事件。分割提交后的判定失败附在 `completed` 结果的说明里。

### 3. CLI 负责进程初始化与命令输出

- CLI 在加载 Runner 前钉卡、限线程。它保留原有子命令、末行 JSON 和退出码。分割异常由 CLI 转为失败结果。 [CLI](../../app/services/inference/offline/cli.py)
- JobService 继续通过 CLI 子进程执行后台作业，从末行 JSON 更新作业状态。 [JobService](../../app/services/inference/offline/service.py)

## 变更效果

- 七个策略用合成检测帧和打桩模型对照改造前后的时间戳、模型输入、逐帧概率及完整动作段，结果一致。
- 真实 CLI 子进程测试验证了 JobService 调用和 stdout 结果解析。
- 用真实 DINOv2 和 MS-TCN2 权重在 CPU 上处理 45 帧合成输入，写出了逐帧概率与手部嵌入缓存，并完成规则判定。
- `pytest tests/` 共 1288 项通过。

## 遗留风险 / 后续任务

- **短序列判定。** 12 帧输入复现了现有判定错误。规则平滑窗为 15 帧时，平滑后的数组与帧可见性数组形状不匹配。Runner 会在 `completed` 的说明中记录判定失败。 [规则平滑](../../app/services/inference/offline/compliance/evidence.py#L228)
- **重跑时概率写入失败。** 如果替换概率文件失败，上一次的文件仍在，自动判定可能读入旧概率。该情形已用模拟写入失败复现。 [Runner](../../app/services/inference/offline/runner.py#L99) · [规则判定](../../app/services/inference/offline/compliance/checker.py#L94)
