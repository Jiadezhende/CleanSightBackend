# 检测帧率判据改为剔除断流缺口后的平均间隔，修 25fps 源被误判为 12.9fps

> **变更状态**：生效中（2026-10-17）
> **知识库**：待沉淀

## 概述

[`resample.input_fps`](../../app/services/inference/resample.py) 与 `resample_by_ts(strict=True)` 的帧率判据由「相邻 ts 间隔的中位数」
改为「剔除断流缺口（> 2×中位间隔）后的平均间隔」。抽帧逻辑与 `tol` 不变。mstcn2-role-embed 的低帧率告警、
nodep GRU 的 strict 校验都走这个判据。

## 变更背景

- **现象**：线上 mstcn2-role-embed 作业报「输入帧率 12.994fps」。同一现象在未合入分支 `feature/offline-v5-bundle-mstcn2` 的
  5a84112 已查明：detections 的 ts 是解码线程的到达墙钟，约 25fps 的源经 ffmpeg `fps=30` 每 5 帧补 1 帧、补帧与原帧几乎同时到达，
  再隔 1 留 1，间隔呈 80 / 80 / 40 ms 循环；中位数落在 80 ms，把平均 15fps 判成 12.9fps。
- **承接**：本次把 5a84112 的判据与用例移植到当前代码，并更正 [20261017_OFFLINE_CLEAN_MSTCN2_ROLE_EMBED.md](20261017_OFFLINE_CLEAN_MSTCN2_ROLE_EMBED.md)
  追加段「13fps 会拉长窗口」的结论——那只在真实欠采样时成立。

## 方案详情

| 判据 | 25fps 源节律（80/80/40） | 真实欠采样 / 丢帧 | 中途断流 10 s |
|------|------------------------|------------------|---------------|
| 中位数（旧） | 12.9fps，误判 | 判低 | 不受影响 |
| 剔缺口后平均（新） | 15fps | 判低（丢帧会如实拉低平均） | 缺口剔除，不受影响 |

## 变更效果

| 项 | 结果 |
|----|------|
| 新增 / 改写用例 | 80/80/40 节律 strict 放行且 `input_fps` = 15；断流不拉低；真实 10fps 仍拒；丢帧如实计入；role-embed 遇 25fps 节律不告警 |
| 全量 `pytest tests/` | 1075 passed |

## 遗留风险 / 后续任务

无。
