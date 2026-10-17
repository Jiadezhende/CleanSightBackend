# 删除旧离线策略文件 impl/clean.py

> **变更状态**：生效中（2026-10-17）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-10-17）

## 概述

删除 `app/services/inference/offline/impl/clean.py` 与只测它的 `tests/test_offline_clean_legacy.py`。其中 5 个策略已在前几批迁成
自包含包并逐位核对（`clean_mstcn_bilstm` / `clean_asformer` / `clean_bigru` / `clean_gru_nodep`，以及从分支补进来的 `clean_mstcn2_bundle`），
YAML、文档、物料都已指向新包（[20261017_OFFLINE_MATERIALS_ONE_DIR_PER_MODEL.md](20261017_OFFLINE_MATERIALS_ONE_DIR_PER_MODEL.md)）。
在线 detection / temporal 的 CLEAN 实现 docstring 里「离线分割见 offline/impl/clean.py」改指新包。

## 变更效果

| 项 | 结果 |
|----|------|
| 仓库内对 `offline.impl.clean` 的引用 | 只剩新包 `__init__` 里的来源说明 |
| 全量 `pytest tests/` | 1115 passed（少的 8 条是随旧文件删除的旧类用例，新包各有对应用例） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 已落盘的旧 run 分段 producer 仍是 `CleanNodepGRUSegmenter` | 仅展示；重跑后变 `CleanGRUNodepSegmenter` | 无 |
| 回滚 | 需要旧扁平物料路径 | `git revert` 本提交即可恢复旧文件；物料按上一批记录反向 `mv` |
