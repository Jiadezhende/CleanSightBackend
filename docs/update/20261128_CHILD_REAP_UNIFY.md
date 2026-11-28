# 子进程收尸收敛为共享 `kill_process_group`

> **变更状态**：生效中（2026-11-28）
> **知识库**：待沉淀

## 概述

推理代理与渲染代理各自的「收子进程 + 拆队列」收尸代码，收敛为 `app/services/utils/process_group.py` 的 `kill_process_group`。两个调用点的行为与日志文案不变，影响面仅限停机 / 重启失败路径。

## 变更背景

- **现状 / 痛点**：同一段收尸知识有两份。`RemoteInferProxy._kill_child` 是 20260902 挂死事故的修复结论（队列三步缺一步就挂死解释器退出）；渲染代理落地时整段照抄，注释写着「理由同 RemoteInferProxy._kill_child」。下次改一处漏一处，代价是事故复现。
- **触发来源**：对全仓 9 处子进程实现的横向盘点——收尸协议是其中唯一逐字重复、且复制代价最高的一段（其余重复项：子进程侧壳 3 份、一次性外部命令 2 份，本期不动）。
- **承接**：建立在 [20260902 收尸修复](20260902_INFER_PROXY_EXIT_HANG.md)与 [20261013 渲染代理](20261013_VIZ_RENDER_PROXY.md)之上，两者的行为语义均不变。

## 方案详情

### 全景：一次故障收尸

```text
监督 / 停机路径判定子进程失败
  → 取 _proc_lock（与 spawn 互斥）
  → kill_process_group(procs, queues=(req_q, resp_q), break_reader_of=req_q)
        ① 每个进程 terminate → join(2s) → kill → join(2s)
        ② req_q._reader.close()      ← 断本进程没人用的读端，逼阻塞的 feeder 拿 EPIPE 自退
        ③ req_q / resp_q: cancel_join_thread() → close()
  → 调用方清在途 / reset 环 / 计丢帧 / 清 _child_ready
  → 释放 _proc_lock，交给退避重启
```

**顺序是硬约束**：② 必须在 ③ 之前——否则阻塞中的 feeder 拿不到 EPIPE，撤 join 只是让进程能退、僵尸线程还在。①→②→③ 的整体顺序沿用原实现，不做调换。

| 部件 | 落在哪 | 详见 |
|---|---|---|
| `kill_process_group` | `app/services/utils/process_group.py` | 本节 |
| 调用点（推理） | `app/services/inference/online/detection/infer_proxy.py` | §1 |
| 调用点（渲染） | `app/services/inference/online/visualization/render_proxy.py` | §1 |
| 单测 | `tests/test_process_group.py` | §2 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|---|---|---|
| 抽收尸函数（采用） | 新增 1 个 stdlib 模块，两个调用点各 6~7 行 | 两边逐字相同的只有这一段，抽它不需要参数化任何域内策略 |
| 统一 `WorkerProcess` 基类 | 要参数化一次性/常驻、Pipe/Queue、watch/drain、重启策略、健康判据、指标 label、停机顺序 | 否决：抽象泄漏面大于省下的行数 |
| 不抽，只写 KB 清单 | 零代码风险 | 否决：知识仍两份，改一处漏一处的人为风险不降 |

### 1. 收尸函数与两个调用点

- `process_group.py`：`kill_process_group(procs, *, queues, break_reader_of, logger, label)`。进程项 `None` 跳过；任何一步失败只记一条 `[label] kill child failed:` 并继续，收尸路径不为异常改变语义。
- `infer_proxy.py`：`_kill_child` 在 `_proc_lock` 内调它，之后 `_child_ready.clear()`；`_proc_lock` 与 `_spawn_child` 的互斥说明留在原处。
- `render_proxy.py`：`_kill_children` 同上，之后 `self._procs = []`。
- 放 `app/services/utils/` 的理由：两个消费者分属 detection / visualization 两个子包，都向下依赖 utils；模块只 import `logging` / `typing`，不 import 兄弟 service（`LAYER_PACKAGES` 守）。已在 `test_import_hygiene.BUDGET` 登记（stdlib only）。

### 2. 单测

`tests/test_process_group.py` 6 例，不 spawn 真进程：terminate 即退不补 kill、SIGTERM 不退升 kill、杀不死仍拆队列、读端先于队列拆、异常吞掉但记日志、`None` / 普通 `queue.Queue` 容错。

## 变更效果

| 维度 | 变更前 | 变更后 |
|---|---|---|
| 收尸实现 | 2 份（含长注释） | 1 份 + 调用点各 6~7 行 |
| 队列三步知识 | 两处注释各说一遍 | 模块 docstring 一处 + 链接 20260902 记录 |
| 行为 / 日志 | — | 不变；`queue.Queue` 等无 `_reader` 的对象仍容错跳过 |

**自测结果**

| 项 | 结果 |
|---|---|
| `tests/test_process_group.py`（新增） | 6 passed |
| `tests/test_infer_proxy.py` / `tests/test_render_proxy.py` | passed |
| `tests/test_viz_worker_render_proxy.py` / `tests/test_import_hygiene.py` | 114 passed |
| 全量 `pytest tests/` | 1310 passed, 1 failed |

失败项 `test_frame_ring.py::test_spawned_process_reads_frames` 是 `_winapi.CreateNamedPipe` / `CreateFile` 上的 WinError 5：本机执行环境禁命名管道；未碰 `frame_ring`，需在无该限制的环境复跑。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|---|---|---|
| 收尸后不回头确认 feeder 线程真的退出（0902 记录已列的遗留） | 进程能退，但僵尸 feeder 与在途 buffer 静默活到进程结束 | 本次只搬家不改行为；要补只补 `process_group` 一处——这正是抽出来的收益 |
| 子进程侧壳（忽略 SIGINT + `basicConfig`）仍 3 份、一次性外部命令仍 2 份 | 同类知识继续多处维护 | 等第三个使用方出现再抽 |
