# 离线分析执行模块改名为 worker

> **变更状态**：生效中（2026-11-24，当前分支）
>
> **知识库**：待沉淀

## 概述

离线分析模块由 `offline/runner.py` 改为 `offline/worker.py`，执行类由 `OfflineRunner` 改为 `OfflineWorker`，日志标签同步为 `[OfflineWorker]`。作业服务通过原有 CLI 子进程入口执行，`run` 和 `check` 由 CLI 调用 `OfflineWorker`。 [执行模块](../../app/services/inference/offline/worker.py) · [CLI](../../app/services/inference/offline/cli.py)

## 变更效果

- `pytest tests/` 共 1288 项通过。
- `integration_tests/test_offline_job_subprocess.py` 验证了作业服务启动真实 CLI 子进程并解析结果。
