# 离线作业服务直接调度 Worker

> **变更状态**：生效中（2026-11-28）
> **知识库**：待沉淀

## 概述

后台离线作业不再启动 CLI 子进程：`OfflineJobService` 用 multiprocessing spawn 直接以 `analysis_child.worker_main` 为入口跑 `OfflineWorker.run(spec)`，结果经单向 Pipe 回传。CLI 保留为人工测试/排障/查询入口，`run` 与后台共用同一执行函数。对外作业对象、状态机、串行队列、1800s 超时与落盘产物不变。

## 变更背景

- **现状 / 痛点**：服务把作业拼成 `python -m app.services.inference.offline.cli run …`，再从子进程 stdout 末行解析 JSON。参数与结果在服务与 CLI 之间各转换一次，CLI 成了后台执行的隐式协议——CLI 的任何输出改动都会连带影响后台作业。
- **触发来源**：提案 [20261124_OFFLINE_RUNNER_DIRECT_DISPATCH_PROPOSAL](20261124_OFFLINE_RUNNER_DIRECT_DISPATCH_PROPOSAL.md)。
- **承接**：执行模块已统一为 `OfflineWorker`（[20261124_OFFLINE_ANALYSIS_PIPELINE_REFACTOR](20261124_OFFLINE_ANALYSIS_PIPELINE_REFACTOR.md)）；本次只替换「谁启动它」，`worker.py` 一行未动。

## 方案详情

### 全景：一次后台离线作业

```text
submit → SerialTaskQueue（串行，一次一个） → analysis_proxy.spawn(task, step, run, threads)
      → spawn 子进程（daemon）: analysis_child.worker_main
           → 忽略 SIGINT / basicConfig / chdir(仓库根) / 降优先级
           → configure_process: 钉 CUDA_VISIBLE_DEVICES → 首次 import torch → set_num_threads(2)
           → OfflineWorker.run(spec)
           → 单向 Pipe 回传 {status, producer, segment_count, alarm_count, message}
      ← service._watch: 存活期持续 poll 结果 + 等退出；超时 / 取消 / 停机 → kill
      → 状态收口（completed / skipped / reclaimed / failed / cancelled）
```

| 步骤 / 部件 | 落在哪 | 详见 |
|---|---|---|
| 子进程入口与运行环境 | [`analysis_child.py`](../../app/services/inference/offline/analysis_child.py) | §1 |
| 父侧句柄与 spawn | [`analysis_proxy.py`](../../app/services/inference/offline/analysis_proxy.py) | §2 |
| 队列、等待与状态收口 | [`service.py`](../../app/services/inference/offline/service.py) | §3 |
| 人工 CLI | [`cli.py`](../../app/services/inference/offline/cli.py) | §4 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|---|---|---|
| 每作业一个 spawn 子进程（采用） | 每作业一次解释器启动 + 模型重载（实测含 torch 冷启动约 11s） | 离线作业分钟级、人工触发，开销占比小；进程退出即归还权重与显存，无作业间 CUDA / 缓存残留；与「一次进程一次 run」的既有语义最接近 |
| 常驻离线 worker 子进程 | 需要作业协议、就绪屏障、监督重启、作业间状态清理 | 否决：当前作业频率与时长不支持这套复杂度 |
| 后端进程内起线程跑 | 无法 kill（取消/超时/停机失效）、`torch.set_num_threads` 污染全局、与 torch/CUDA-free 主进程冲突、崩溃带崩后端 | 否决：串行只约束并发度，不解决进程级隔离 |

命名沿用仓库既有的「子进程侧 / 父进程侧」成对惯例（`stage_worker.py`/`infer_proxy.py`、`render_child.py`/`render_proxy.py`）：

| 方案 | 结论 |
|---|---|
| `analysis_child.py` + `analysis_proxy.py`（采用） | 域词「离线分析」对齐文档与 `OfflineWorker`；子侧薄模块独占「钉卡先于重 import」的边界 |
| spawn target 写进现有 `worker.py`，只新增 `analysis_proxy.py` | 否决：spawn 会在 target 执行前 import 该模块，钉卡顺序将押在「`worker.py` 顶层碰巧 torch-free」上，且编排模块混入 signal/日志/chdir/优先级等进程壳 |
| `dispatch.py` / `worker_child.py` | 否决：前者只说机制不说角色、CLI import 语义不清；后者与 `worker.py` 名字过近易混 |

### 1. `analysis_child.py` — 子进程侧入口与运行环境（全景第 2 步）

模块顶层只 import stdlib（`logging/os/signal/sys/pathlib`）；`worker` / `torch` / `app.settings` 一律函数体内 import。**这是硬约束**：spawn 会在 target 执行前先 import 本模块，`CUDA_VISIBLE_DEVICES` 必须早于整条 import 链，否则 torch 可能先看到全部卡。

- `configure_process(threads)`：原 CLI 的 `_pin_cuda_device` + `_configure_run_process` 合并，顺序不变（钉卡 → 首次 `import torch` → `set_num_threads`）。
- `execute(task, step, run, threads) -> dict`：先配置再 `from .worker import OfflineWorker`，跑一次 `run()`；异常在此转成 `status="error"` 的 5 字段结果（CLI 与子进程共用，异常→失败只此一处）。
- `worker_main(send_conn, …)`：spawn target。初始化子进程（忽略 SIGINT、`logging.basicConfig` 走继承的 stderr、`os.chdir(仓库根)` 保住模型相对路径契约、平台相应的低优先级）→ `execute` → `send` → 关闭写端 → 按 `status` 退出 0/1。
- 低优先级：Windows `SetPriorityClass(BELOW_NORMAL)`，POSIX `os.nice(15)`，等价原 `nice -n 15` 命令前缀；失败只告警。

### 2. `analysis_proxy.py` — 父侧句柄与 spawn（全景第 2 步收尾）

- `spawn()`：`get_context("spawn").Pipe(duplex=False)` + `Process(target=worker_main, args=(send_conn, …), daemon=True)`；`start()` 成功后**父进程立即关闭写端**（否则永远读不到 EOF）；启动失败关两端并抛出。
- `WorkerProcess`：`pid / exitcode / is_alive / join / kill / poll_result / at_eof / close`。`poll_result` 在子进程存活期间持续读通道，因此超出管道缓冲的长 `message` 不会把子进程堵在 `send`；`close()` 只在进程已退出时释放 `Process` 句柄，存活时留给 daemon 收尾。

### 3. `service.py` — 队列、等待与状态收口（全景第 3 步）

- 删除 CLI 命令拼接、stdout/stderr 临时文件与末行 JSON 解析（`subprocess/shutil/tempfile/sys` 全部移除）；构造改为注入 `spawner`（默认 `analysis_proxy.spawn`，测试注入假件）。
- `_watch`：存活期 poll 结果，收到结果或 EOF 后改为 join 等退出；超时 `kill` 后给 `KILL_GRACE_S = 5.0` 回收宽限，仍不退出则记 ERROR 并按终止原因先收口，避免杀不掉的进程把串行队列永久卡死。
- 状态映射（**终止原因优先于子进程返回值**）：

| 子进程情形 | 作业终态 |
|---|---|
| 有效结果 + exitcode 0 | `completed` / `skipped` / `reclaimed`（取结果字段与 message） |
| 有结果但 status=error 或 exitcode≠0 | `failed`，message 取结果 message |
| 无结果且已退出 | `failed`，message 带 exitcode |
| 取消 / 停机先到 | `cancelled` |
| 超时先到 | `failed`「超时」 |
| `spawn` 抛异常 | `failed`「子进程启动失败」 |

### 4. `cli.py` — 人工入口（全景第 4 步）

`run` 改为调用 `analysis_child.execute` 后打印同一行 JSON；`check / query / materials` 与所有参数、输出、退出码保持不变。CLI **不再被后台调用**，也不被包内任何模块 import。

### 5. 保留项（刻意不动）

- `worker.py` / `OfflineWorker` / `OfflineRunResult` 与结果 5 字段。
- 作业对象 schema、状态机、`QUEUE_SIZE=20`、`JOB_TIMEOUT_S=1800`、`HISTORY=200`、运行中 run 409、按 run 去重、`_preflight_materials`。
- `/admin/offline/*` 三个端点、落盘产物（`temporal.jsonl` / `label_probs.npz` / `alarms.jsonl`）。
- CLI 子命令、参数、末行 JSON、退出码。

### 数据通道

| 通道 | 谁写 | 谁读 | 说明 |
|---|---|---|---|
| 结果 Pipe（child→parent） | 子进程 `worker_main` | 父进程 `WorkerProcess.poll_result` | 每作业一次，5 字段 dict；子进程退出即 EOF |
| 诊断日志（stderr） | 子进程 `basicConfig` | 后端进程的 stderr / 终端 | 带 `[offline-worker]` 前缀，与在线推理子进程同款 |
| 终止信号 | 父进程 `Process.kill()` | 子进程（SIGKILL / TerminateProcess） | 取消、超时、停机共用 |

## 变更效果

| 维度 | 变更前 | 变更后 |
|---|---|---|
| 后台入口 | `python -m …cli run` 子进程 | spawn Python 函数入口，CLI 不参与 |
| 参数 / 结果传递 | 命令行参数 + stdout 末行 JSON | 函数入参 + 单向 Pipe dict |
| 失败信息 | 末行 JSON；无 JSON 时取 stderr 末尾 2000 字 | 结果 message；无结果时带 exitcode |
| 取消 / 超时 | `Popen.kill` + `wait` | `Process.kill` + 5s 回收宽限 |
| 钉卡 / 限线程 | CLI 内（隔离后才 import worker） | `analysis_child`（顺序不变，新增导入纪律门禁） |
| CLI | 后台唯一执行方式 | 人工入口，与后台共用 `execute` |

**自测结果**

| 项 | 结果 |
|----|------|
| 相关单测（离线作业服务 / admin 路由 / 两个新模块 / CLI） | 80 passed |
| 导入纪律门禁（新增 `analysis_child`、`analysis_proxy` 两条预算 + 禁 worker 连带） | 108 passed |
| `integration_tests/test_offline_job_subprocess.py`（真 spawn） | PASS：子进程读真实 YAML 报「step 987 未在推理配置中定义」→ 作业 `failed`，message 来自结果通道 |
| 全量 `pytest tests/` | 1305 passed |
| CLI 冒烟：`materials` / 未配置 step 的 `run` | 退出码 0（物料齐全）；未配置 step → 末行 error JSON + 退出码 1 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|---|---|---|
| 子进程按生产启动方式会先以 `__mp_main__` 重导入 `app.main`，且每作业重载模型 | 每作业启动多 1–2s + 一份 FastAPI 装配内存；实测含 torch 冷启动约 11s | 离线是分钟级作业，接受；若作业频率上升，再评估常驻 worker |
| 真实 GPU 作业未在本次验证（无真实 run + 生产配置环境） | 钉卡顺序沿用原 CLI 代码路径（仅移动位置），由导入纪律门禁守「torch 在设置环境之后」；真实 CUDA 可见性与显存归还未实测 | 需用户确认后在开发环境用真实 run 跑一次后台作业，核对产物与 CLI 手动跑一致 |
| spawn 前提：后端必须用 `python -m app.main`（`__main__` 有守卫）启动 | 用 `uvicorn` 控制台脚本直启时 spawn 会重跑该脚本 | 与在线推理子进程同一既有前提，未新增风险；`start_backend.*` 已是该方式 |
| 知识库与实现分歧：`SERVICE_INFERENCE.md` 仍写「service 只把 CLI 当子进程命令」、`TESTING_MAP.md` 仍写 `FakeLauncher` | 不影响运行，只影响后续阅读 | 待知识库维护时随本记录沉淀 |
