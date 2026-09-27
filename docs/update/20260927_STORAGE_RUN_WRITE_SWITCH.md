# 写侧切到 run 目录：`start_run` 分配 run，recording / 离线只写自己的 run，首写自清与认领表删除

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 3 期；新数据落 `{step}/{run_id}/`，旧布局数据读侧不可见
> **知识库**：待沉淀

## 概述

`/api/start` 在 `lock_for` 内经 `runs.allocate` 分配 run 并建目录，CQ 改收 `run: RunIdentity`。recording 的段与检测结果、离线的 temporal / label_probs 都只写所属 run 的目录。存储层写口只收 RunIdentity，读口的旧 `(task_id, step_id)` 形态经 `runs.query` 转发到最新可见 run。随之删掉已无调用方的旧实现：`LegacyStep`、recording 的代次校验 + 首写自清 + 认领表 + `forget_*`、`hls.delete` / `inference.delete`、`_root.path` 的 `domain` / `create` 参数。

## 变更背景

- **承接**：[落盘按运行分目录提案](20260927_STORAGE_RUN_DIR_PROPOSAL.md) 第 3 期，建立在第 1 期 [`_fs` 原语](20260927_STORAGE_FS_PRIMITIVES.md)、第 2 期 [RunIdentity + runs](20260927_STORAGE_RUN_IDENTITY.md) 之上。
- **范围比提案原计划大**：第 2 期的 `LegacyStep` 只为让旧调用在第 2 期继续打到旧布局。写侧切换后它、首写自清、认领表、两个 `delete`、`_root.path(create=)` 都没有调用方了，因此在本期一并删除（提案原排第 5 期），提案 §8 已同步修改。

## 方案详情

### 全景

```text
/api/start ─ lock_for ─┬ 幂等 / 重启时先 stop_run
                       ├ runs.allocate(task, step) → RunIdentity，mkdir {step}/{run_id}/   失败 → AppError(500)
                       ├ ClientQueues(run=run, ...)                                          CQ 身份此后不可变
                       └ client_manager.set → start_workflow → start_stream

写  recording 两条队列：_SegmentJob / _DetectionJob 只带 run → hls.insert_segment(run) / inference.append_detections(run)
    离线 runner：入口 runs.query(task, step) 一次 → 全程读写这个 run
    写口只建 {run}/{domain}/ 这一级；run 目录不在 → FileNotFoundError → 队列记 error 吞掉

读  读口 f(task_id, step_id, ...) ──legacy_reader──→ runs.query(task, step) → 最新可见 run
                                                     查不到 → RunIdentity(task, step, 0)，目录永不存在，读到空
```

| 部件 | 落在哪 |
|------|--------|
| 分配 run、删拆除第 4 步 | [`run_control.py`](../../app/services/run_control.py) |
| CQ 身份 | [`client/queues.py`](../../app/services/client/queues.py)：`run` 字段；`task_id` / `step_id` 为只读转发属性 |
| recording | [`recording/service.py`](../../app/services/recording/service.py) |
| 离线 | [`offline/runner.py`](../../app/services/inference/offline/runner.py) |
| 存储层 | `_root.py`（`domain_dir` 只收 RunIdentity、`legacy_reader`）、`hls/*`、`inference/*` |
| 对外契约 | [`docs/api/api.md`](../api/api.md) `/api/start` 的 500：补上「落盘目录建不出来」 |

### 1. `start_run`

- `runs.allocate` 在 `lock_for` 内、`ClientQueues` 构造之前。`OSError` 包成 `AppError`（500）。
- 行为变化：start 现在会碰盘。重启场景下旧 run 先停，分配失败时旧 run 已经停了（已写进 api 文档）。
- 拆除的第 4 步 `recording_service.forget_task` 删除，没有代次记录需要回收了。

### 2. recording

- 删除：代次校验 ①（新 CQ 换上后丢弃旧段）、首写自清 ②（`hls.delete` / `inference.delete`）、`_claimed_hls` / `_claimed_detections`、`forget_task` / `_forget_hls` / `_forget_detections`、模块不变式 4。
- `_write` / `_write_detections` 各只剩一次调用。旧一代迟到的段写进它自己的 run：以前这些尾段会被丢弃，现在补全旧录像的结尾。
- 「未绑定 run」只剩 `cq.run is None` 一个判据，6 处「缺 task_id / step_id」判断随之收掉。
- `_pending_flush` 的回收从 `_forget_hls` 挪到 `flush_residual(cq)` 的拆除路径（`until_ts=None`）：按对象身份 pop 本 cq 的请求。`stop_run` 持 `lock_for`，期间不会有新一代登记同一个键。

### 3. 离线 runner

入口 `runs.query(task, step)` 解析一次，之后 `read_detections` / `write_label_probs` / `read_temporal` + `write_temporal` 全用这个 RunIdentity。没有可见 run → `skipped`。提交接口、`--run-id`、409、`reclaimed` 仍按提案放在第 4 期。

### 4. 存储层

- `_root.domain_dir(run, domain, create=)` 只收 RunIdentity，非 RunIdentity 抛 `TypeError`；`path()` 只定位 task / step，不再建目录。
- 写口（`insert_segment` / `append_detections` / `write_temporal` / `write_label_probs`）只收 RunIdentity，传旧形态会得到 `TypeError`。
- 读口挂 `legacy_reader`。查不到 run 时用 `run_id=0` 兜底：分配出的 run_id 是微秒时间戳，`{step}/0/` 永不存在，旧调用方拿到的是空列表、None 或不存在的路径，与原先「step 没数据」一致。第 4 期迁完读侧调用点后删除。

### 5. 测试

- `tests/conftest.py` 新增 autouse 的 `_isolate_storage_root`：`start_run` 现在会建目录，没显式要 `tmp_storage` 的用例也不能写真实 `database/`。
- `tests/factories.py`：`make_cq` 改为构造 `run`；新增 `make_run(task, step)`，返回该 step 最新的 run，没有就分配一个；`seed_hls_segments` 落进 `make_run`，并写 `metadata.json` 让它可见。
- `test_recording_service.py`：删掉代次校验、懒惰 supersede、forget_task 三组用例，换成按 run 落盘（旧一代迟到段写进自己的 run）与拆除路径回收挂起 flush 的用例。
- 存储层测试改用固定 run；`hls.delete` / `inference.delete` 的用例随函数删除。

## 变更效果

| 冲突（提案编号） | 现在 |
|------|------|
| #1 新一代首写 vs 离线结果写入 | 离线写的是入口解析出的 run，新一代写自己的 run，盘上不相交 |
| #3 TTL 回收 vs 离线写入 / 迟到残段 | 写者不建 run 目录，回收后的写入 `FileNotFoundError`，不再重建空壳 step |
| #5 首写自清失败导致新旧混写 | 写侧不再删除 |
| #2、#4 | 未闭合，第 4 期处理（离线 409、读侧整次锁定 run） |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 819 passed |
| `integration_tests/test_hls_frame_roundtrip.py`（真 ffmpeg，不碰 DB / 告警） | 9 / 9：`insert_segment(run)` 落进 run 目录，旧形态读口经 `runs.query` 读回，ts 与像素逐帧对齐 |
| dev 端到端（启停、同 step 重启、回放、离线提交） | **未跑**：要起真实 RTSP 流并写 dev DB，需确认后执行 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 旧布局数据读侧不可见 | 上线前 15 天内的回放和离线结果看不到；盘上残留由 TTL 清掉 | 按提案接受，需要时写一次性迁移脚本 |
| 同 step 重启后，新 run 的 `detections.jsonl` 约 1 s 就可见，首段 hls 要 10 s 以上 | 这期间不带 `run_id` 的回放返回空，不再显示上一次录像 | 按提案接受；第 4 期读口接收 `run_id` 后可点名旧 run |
| 离线提交时该 run 正在运行 | 按提交时已落盘的部分检测结果出结论（冲突 #2） | 第 4 期提交时 409 |
| 读侧跨请求仍可能跨代 | 回放页的 playlist、timeline、temporal 各自解析最新 run | 第 4 期读侧整次锁定 + 对外 `run_id` |
| 旧形态读口的 `run_id=0` 兜底 | 迁移期专用，读侧调用点不迁完就一直在 | 第 4 期迁完调用点后删 `legacy_reader` |
