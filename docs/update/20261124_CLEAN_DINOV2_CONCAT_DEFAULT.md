# CLEAN 默认离线策略切换为 DINOv2 Concat

> **变更状态**：生效中（2026-11-24，随仓库配置）
> **知识库**：待沉淀

## 概述

CLEAN 的 stage 2 默认启用 `CleanDINOv2ConcatSegmenter`，采用 857 维输入、五类输出和 pp5 后处理。 [推理配置](../../config/inference_config.yaml)

## 变更背景

后端已接入训练侧提供的 DINOv2 手部嵌入直接拼接基线。此次切换 stage 2 的默认离线模型，以便通过同一作业入口使用该基线。

## 方案详情

| 环节 | 配置与行为 |
|---|---|
| 策略与物料 | `CleanDINOv2ConcatSegmenter` 从模型根下的 `clean-offline-dinov2-concat/model.pt` 加载 fold0 / seed1 权重，并复用 `backbones/dinov2_vits14/`。 |
| 推理参数 | CUDA，150 裁图/秒，首帧起前 900 秒，最短段长 5 帧。 |
| 合规核对 | stage 2 规则组引用的类别均在新策略的五类标签内，可直接消费逐帧概率。 |

## 变更效果

新增离线作业在 stage 2 使用五类 DINOv2 Concat 基线。重跑后，概率与分段按该 run 的正常离线作业流程替换。

本地物料检查通过。隔离样本用仓库默认配置得到 30×5 逐帧概率，合规试运行完成五项规则判定。全量单元与组件测试 1286 项通过。

## 遗留风险 / 后续任务

切换环境时，通过 `python -m app.services.inference.offline.cli materials` 检查实际模型根下的权重和骨干文件。
