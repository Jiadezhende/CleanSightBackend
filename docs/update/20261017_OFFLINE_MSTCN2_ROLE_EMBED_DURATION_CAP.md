# mstcn2-role-embed 离线只推理首帧起前 15 分钟，超长 step 截断

> **变更状态**：生效中（2026-10-17）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-10-17）

## 概述

[`CleanMSTCN2RoleEmbedSegmenter`](../../app/services/inference/offline/impl/clean_mstcn2_role_embed/segmenter.py) 新增参数 `max_duration_s`（YAML 默认 900）：
检测序列从首帧起超过该时长时，只保留前 15 分钟（闭区间）推理，其余截断并 WARNING；`null` = 不截断。
截断发生在取帧之前，raw 录像只解码前 15 分钟，嵌入、特征、`label_probs` 与分段都只覆盖这段。

## 变更背景

- **人定**：检测时长不超过 15 分钟，超时截断、只看前 15 分钟，避免超长 step 占用过多资源。
- **资源上界**：15 分钟 ≈ 13500 帧、约 2.6 万次手部裁剪；GPU 限速 150 次/s 时嵌入段约 3 min，远低于 1800 s 作业超时；
  手部嵌入缓存约 40 MiB。

## 方案详情

```text
preprocess: _truncate(frames, max_duration_s) → _warn_low_rate → resample_by_ts(15) → 取帧 / 嵌入 / 特征
```

- 截断以**首个检测帧的 ts** 为起点；时长 ≤ 上限时原样返回，不复制序列。
- 截断后的手与缓存逐行对应，同一 run 重跑仍能命中缓存；改 `max_duration_s` 会让手的行变化，自动重算覆盖。

## 变更效果

| 项 | 结果 |
|----|------|
| 新增用例 | 20 分钟序列只留前 900 s 并告警；短序列 / 不截断原样返回；`max_duration_s: 0` 构造即失败；随仓 YAML 为 900 |
| 全量 `pytest tests/` | 1071 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 15 分钟之后的动作不出分段 | 超长 step 后段的漏步 / 乱序看不到 | 人定的取舍；需要时调大 `max_duration_s` |
