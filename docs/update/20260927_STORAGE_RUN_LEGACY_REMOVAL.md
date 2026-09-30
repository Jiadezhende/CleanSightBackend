# 删除 run 迁移期的两处转发：读口的 `(task_id, step_id)` 形态与 CQ 的身份属性

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 5 期，全部分期完成
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[SERVICE_CLIENT_STATE.md](../kb/SERVICE_CLIENT_STATE.md)（2026-09-30）

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

### 追加修正（2026-09-28）：转发属性补删、清掉旧布局描述

- **CQ 转发属性只删了一半**：原提交只删了 `task_id` 上的 `@property`，方法体和 `step_id` property 都还在。`cq.task_id` 因此成了一个 bound method：有读者时不会 `AttributeError`，而是拿到一个恒真、永不等于任何 id 的值。已删干净。
- **旧布局描述清理**：
  - 代码与测试里的 `{step}/hls/`、`{step}/inference/` 改为 `{run}/…`；
  - 删掉「读侧不回落旧平铺布局」「旧布局残留随 step 回收」等说明，以及只为旧布局造数的用例（`test_legacy_layout_is_invisible`、TTL 的 `_make_legacy_step` 分支）；
  - `_root.DOMAINS` 去掉已无使用方的 `"lab"`（lab 临时件在 `.lab_exports/`）；
  - 回放 / 整段导出缺 init 的 503 文案与 `docs/api/{traceback,lab}.md` 去掉「旧格式产物」这一原因：run 目录全由新代码写出，只剩首段在 transcode 这一种可能。
- 全量 `pytest tests/`：834 passed（减少的 3 条是旧布局用例和 `lab` 域参数化）。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| KB 与新布局不一致 | [DESIGN_STALE_WRITES](../kb/DESIGN_STALE_WRITES.md)、[DESIGN_STORAGE_LAYER](../kb/DESIGN_STORAGE_LAYER.md)、[ARCHITECTURE_STORAGE_AND_SCHEMA](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md) 仍描述 `{step}/{domain}/` 与首写自清 | 下次 KB 维护时融合本系列 update |
| 旧布局数据读侧不可见 | 切换前的回放与离线结果看不到，残留随 TTL 清掉 | 按提案接受 |
