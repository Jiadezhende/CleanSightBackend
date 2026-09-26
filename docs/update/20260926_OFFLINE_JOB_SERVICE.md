# 离线作业服务：SerialTaskQueue 串行 + 子进程跑 CLI

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

新增 `app/services/inference/offline/service.py`（`OfflineJobService`）与单例 `offline_job_service`，挂进 inference 的 `lifespan()`。
提交的离线作业会逐个串行执行，每个作业起一个子进程，运行 `cli run --strict --json`。
本批还没有对外端点，admin 入口放在下一批。

## 变更背景

- **现状**：离线推理只能在服务器上手敲 CLI 触发。多人或多个 step 同时手动运行时，没有任何东西限制并发，CPU 资源会被挤满。
- **触发来源**：前端需要能手动提交离线推理，并且执行必须串行。任务结束后自动触发不在本批范围，挂点见文末。
- **承接**：本批建立在前两批之上：
  - [20260926_OFFLINE_SUPERSEDE_CHECK](20260926_OFFLINE_SUPERSEDE_CHECK.md)：runner 自己用输入戳做换代校验，结果的正确性不依赖本服务。
  - [20260926_OFFLINE_CLI_STRICT_JSON](20260926_OFFLINE_CLI_STRICT_JSON.md)：CLI 提供了可解析的结果输出。

## 方案详情

### 全景

```text
submit(task, step)
  ├─ step 正在 live → ConflictError
  ├─ 同键 queued/running → 返回在途 job（去重）
  └─ 登记 queued → SerialTaskQueue("offline").submit(_execute)     单消费线程 = 一次只跑一个
_execute（队列线程）
  ├─ 持锁：已取消 → 返回；停机中 → cancelled；step 已 live → skipped
  │        否则 Popen 子进程，状态置 running                      ← 与 cancel 同一把锁，kill 不会漏
  ├─ _watch：每 POLL_S 秒 wait 一次；超时 → kill（failed）；step 重新 live → kill（superseded）
  └─ 解析 stdout 末行 JSON → completed / skipped / superseded；否则 failed（优先取 JSON message，没有再取 stderr 尾部）
stop()：kill 运行中的作业 → queue.stop() 排空，剩下的作业逐个记为 cancelled
```

| 部件 | 落在哪 |
|------|--------|
| 服务类 / 状态表 / 子进程 | [`offline/service.py`](../../app/services/inference/offline/service.py) |
| 单例 | [`offline/instance.py`](../../app/services/inference/offline/instance.py) |
| 起停 | [`inference/__init__.py`](../../app/services/inference/__init__.py) 的 `lifespan()`：在 manager 之后起，在 manager 之前停 |
| 单例引用门禁 | `tests/test_import_hygiene.py` 的 `SINGLETONS` |

### 方案选型

| 方案 | 结论 |
|------|------|
| 队列线程起子进程调 CLI（采用） | 保留 CLI 的 CPU 隔离（禁 GPU、限核），以低优先级运行（Windows 用 `BELOW_NORMAL_PRIORITY_CLASS`，POSIX 加 `nice -n 15` 命令前缀）；可以 kill；子进程崩溃或 OOM 不会拖垮后端。代价是每个作业多 1~5 秒的进程启动时间 |
| 在后端进程内直接调 `OfflineRunner` | 否。`torch.set_num_threads` 作用于整个进程，会影响在线的 CleanOperator；而且作业无法中途取消 |

### 实现要点

- **队列**：直接复用 `SerialTaskQueue`，契约不改。它的「停机排空」语义不会拖慢停机：`_execute` 看到停机标志会立即返回。
- **子进程环境**：
  - `env` 复制自 `os.environ`，其中包含 `CLEANSIGHT_ENV` 和已加载的 `.env`，所以子进程和后端使用同一个存储根。
  - 另外设置 `CUDA_VISIBLE_DEVICES=""` 和 `PYTHONIOENCODING=utf-8`。
  - `cwd` 设为仓库根目录。
- **输出**：stdout 和 stderr 写入临时文件，不用管道，避免输出多时把子进程写阻塞。
- **状态**：只放在内存里；已结束的作业保留最近 `HISTORY=200` 条。
- **模块常量**：`THREADS=2`、`QUEUE_SIZE=20`、`JOB_TIMEOUT_S=1800`、`POLL_S=1`。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_offline_job_service.py` 共 22 条 | passed。前 21 条用假子进程，覆盖串行、去重、live 拦截、运行中换代 kill、取消、超时、非 0 退出、启动失败、停机、重启。第 22 条起真实 CLI 子进程，存储根经环境变量传给子进程 |
| 全量 `pytest tests/` | 957 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 还没有对外入口 | 前端暂时无法提交 | 下一批加 admin 端点和 admin tab |
| 作业状态不持久化 | 后端重启后排队中的作业丢失 | 手动触发场景可以接受；做自动触发时再评估是否需要持久化 spool |
| 自动触发 | 未做 | 在 `stop_run` 末尾调用 `submit` 即可（跳过 `start_rollback` 和 identity-fence 判为 skipped 的情况）；未封口的输入会被 runner 判为 superseded |

---

## 追加（2026-09-26）：去掉三处 live 检查

删掉提交时（409）、开跑前（skipped）、`_watch` 轮询（kill → superseded）三处 `_is_live`，以及 `clients` 注入参数和 `superseded` 状态。
服务不再依赖 `client_manager`；`_watch` 只管超时。调用方只对已停写的 step 提交。
理由：手动触发、并发量极低，先保最简实现；换代 / 回收冲突防护以后按 [DESIGN_STALE_WRITES](../kb/DESIGN_STALE_WRITES.md) 的多版本方案统一做，不在这套临时机制上叠加。
