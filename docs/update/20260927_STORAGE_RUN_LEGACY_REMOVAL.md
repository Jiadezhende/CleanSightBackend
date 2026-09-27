# 删除 run 迁移期的两处转发：读口的 `(task_id, step_id)` 形态与 CQ 的身份属性

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 5 期，全部分期完成
> **知识库**：待沉淀

## 概述

删除 `_root.legacy_reader` 及其在 hls / inference 读口上的挂载：存储层读写口从此只收 `RunIdentity`，传 `(task_id, step_id)` 会得到 `TypeError`。同时删除 `ClientQueues.task_id` / `step_id` 两个转发属性，「未绑定 run」只剩 `cq.run is None` 一个判据。纯删除，运行时行为不变。

## 变更背景

- **承接**：[落盘按运行分目录提案](20260927_STORAGE_RUN_DIR_PROPOSAL.md) §8 第 5 期。这两处转发都是迁移期的过渡：读侧调用点在 4a（[读侧锁定](20260927_STORAGE_RUN_READ_LOCK.md)）与 4b（[离线锁定](20260927_STORAGE_RUN_OFFLINE_LOCK.md)）已改为传 RunIdentity，CQ 身份读者在 4c（[CQ 读者迁移](20260927_STORAGE_RUN_CQ_READERS.md)）已迁到 `cq.run.*`，4c 还验证过去掉属性后全量仍绿。
- 提案原排在第 5 期的其余删旧项（认领表、`forget_*`、两个 `delete`、`_root.path(create=)`、`_latest_step_mtime`）已随第 3 期、4a 提前删掉。

## 方案详情

| 删除 | 落在哪 |
|------|--------|
| `legacy_reader`（查不到时用 `run_id=0` 兜底的读口转发） | `app/storage/_root.py`；`hls/{_layout,_read,_decode}.py`、`inference/{_detection,_temporal}.py` 上的装饰器 |
| `ClientQueues.task_id` / `step_id` 转发属性 | `app/services/client/queues.py` |
| 相关 docstring 里的「迁移期另收旧形态」说明 | `app/storage/__init__.py`、`hls/__init__.py`、`inference/__init__.py`、`hls/_layout.py` |

测试里仍用旧形态读的几处（离线 pipeline、存储层「读不建目录」用例、lab 导出的造数）改为传 run。`test_storage_runs.py` 的迁移期转发用例换成一条：读写口收到 `(task_id, step_id)` 即 `TypeError`，且不建任何目录。

`test_lab_step_exporter.py` 的造数以前能绿是碰巧：旧形态经转发兜底建出了 `{step}/0/` 目录，`make_run` 又把 `0` 当成了最新 run。现改为显式用 `make_run`。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 存储层读写口 | 写口只收 RunIdentity；读口另收 `(task_id, step_id)` | 都只收 RunIdentity |
| CQ 身份 | `cq.run` + 两个转发属性 | 只有 `cq.run` |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 839 passed |
| `integration_tests/test_hls_frame_roundtrip.py`（真 ffmpeg，不碰 DB） | 9 / 9 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 第 4 期的 dev 端到端（换代期间持续回放、离线期间同 step 重启）尚未执行 | 读侧锁定与离线 409 只有单测覆盖 | 人工执行后回填 4a / 4b 记录 |
| KB 与新布局不一致 | [DESIGN_STALE_WRITES](../kb/DESIGN_STALE_WRITES.md)、[DESIGN_STORAGE_LAYER](../kb/DESIGN_STORAGE_LAYER.md)、[ARCHITECTURE_STORAGE_AND_SCHEMA](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md) 仍描述 `{step}/{domain}/` 与首写自清 | 下次 KB 维护时融合本系列 update |
| 旧布局数据读侧不可见 | 切换前的回放与离线结果看不到，残留随 TTL 清掉 | 按提案接受 |
