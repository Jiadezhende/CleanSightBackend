# features_ama 并入 bundle.py：mstcn2-role-embed / mstcn2-bundle 的 bundle 特征只剩一个文件

> **变更状态**：生效中（2026-10-17）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-10-17）

## 概述

两个包的 `features_ama/{clean_bbox_v2,v3,v5}.py` 合并进各自已有的 `bundle.py`（原来只是两行入口包装），删除 `features_ama/` 目录。
文件内按「§ v2 绝对坐标块 / § v3 槽位投影 / § v5 器械轴与拼装」分节，函数名沿用训练仓，开头一张表注明每节出自训练仓哪个文件。
顺带删掉合并后才看清的死分支：`_frame_candidates` 的 list 兼容分支、`.get(name, [])` 默认值（读入层总是为全部类建好分桶）。
两个包的 `bundle.py` 逐字节相同。role-embed 自己的 `features.py`（106 维拼装）不变。

## 变更背景

- **承接**：[20261017_OFFLINE_ROLE_EMBED_REUSE_BUNDLE.md](20261017_OFFLINE_ROLE_EMBED_REUSE_BUNDLE.md)。前三批清理后 v3 只剩一个函数、v4 已删，
  按训练仓版本号拆文件在后端不对应任何可单独选用的东西；`bundle.py` 与 `features_ama/` 又是同一层的两处。

## 变更效果

| 项 | 结果 |
|----|------|
| mstcn2-bundle：43 个视频 vs 训练侧 94 维 | 全部逐位一致 |
| mstcn2-role-embed：示例 vs `expected_features.npy`、43 个视频 + 7 组合成序列 vs 清理前实现 | 全部逐位一致 |
| 真实 Runner 全链路（示例视频） | 5 段不变，缓存命中 |
| 包结构 | role-embed：`bundle.py`（94 列）+ `features.py`（106 维拼装）+ `slots.py` / `roles.py` / `hand_state.py` …；bundle 包：`bundle.py` |
| 全量 `pytest tests/` | 1117 passed |

## 遗留风险 / 后续任务

无。
