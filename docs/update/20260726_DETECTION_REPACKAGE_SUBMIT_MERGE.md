# detection 模块收口：重命名 + entrypoint 并入 stage_pool + 取帧/提交合流单提交者

> **变更状态**：生效中（2026-07-26）。detection 子包完成命名/打包收口与线程模型简化；行为在稳态下与前一版等价，全套 355 passed。
> **知识库**：待沉淀 → `kb/SERVICE_INFERENCE.md`（L1 检测子包文件名与线程模型需更新）、`kb/INFER_LAYER_LAYOUT` 相关段落。
>
> 承接：建立在进程隔离落地之上（[20260726_INFER_PROCESS_ISOLATION_LANDED.md](20260726_INFER_PROCESS_ISOLATION_LANDED.md)）——那次把 GPU 前向拆进 spawn 子进程、走 req_id 异步管线。本次不改推理正确性，只做**命名收口 + 线程模型简化**；该前文里的旧文件名（`infer_worker.py`/`remote_infer.py`/`pool.py`）与 `_inference_loop` 描述均被本次取代。

## 概述

- **改了什么**：三件事——(1) 模块重命名/打包收口；(2) 子进程 entrypoint `run_infer_worker` 从独立文件并入 `stage_pool.py`；(3) 把「组批 + 提交」从 `ModelWorkerService` 的 N 个 per-stage 线程**合流进 dispatcher 单循环**，dispatcher 成为唯一提交者。
- **为什么改**：命名歧义（`pool` 不点明「单 stage」、与 viz `pool.py` 撞名；`remote_infer` 冗长）；`infer_worker` 独立文件被误当「承重隔离」，核实后拆分非承重（见下）；per-stage 提交线程是 2ms 窗口/1ms sleep 的忙自旋，多线程还徒增主进程 GIL 争用——正是拆进程要治的病根。
- **影响面**：仅 detection 子包内部。对外行为稳态等价；线程模型由「1 dispatcher 线程 + N 提交线程」塌成「1 dispatcher 线程」。

## 改动详情

### 1. 重命名 / 打包

| 旧 | 新 | 理由 |
|----|----|------|
| `detection/remote_infer.py` | `detection/infer_proxy.py` | 更短、点明是主进程侧代理（类名 `RemoteInferProxy` 保留） |
| `detection/pool.py` | `detection/stage_pool.py` | 点明每实例=「单 stage 多模型池」；与 `visualization/pool.py` 区分 |
| `detection/infer_worker.py` | 并入 `stage_pool.py` 底部 | 见 §2 |
| `tests/test_remote_infer_proxy.py` | `tests/test_infer_proxy.py` | 跟随 |

### 2. `stage_pool.py` — entrypoint 并入 + 顶层 torch 惰性化

`run_infer_worker`（spawn target）从独立 `infer_worker.py` 并入 `stage_pool.py`（它构建/驱动的就是本文件的 `MultiModelWorkerPool`，同层）。

> **拆分非承重，已核实**：曾以为「entrypoint 必须独立文件」是硬隔离约束，实则不然——`import torch` 本身**不建 CUDA context、不读 `CUDA_VISIBLE_DEVICES`**（驱动在首次 CUDA op / `cuInit` 时才读一次）。所以即便 spawn 子进程重 import 本模块时顶层跑了 `import torch`，只要 env 在**首次前向之前**钉好即可，功能不 break。
>
> 但仍保留「顶层 torch-free」纪律：pool.py 原顶层 `import torch` 改为 [`warmup`](../../app/services/inference/detection/stage_pool.py) 内惰性 import。这是**防御性**的（守住 env-先于-torch 的隔离序、与 `offline/cli.py` 对称），不是硬崩溃防护。验证：`import stage_pool` 后 `torch` 不在 `sys.modules`。

### 3. 取帧 + 提交合流到 dispatcher（单提交者）

**核心决策**：删掉 service 的 N 个提交线程，把「消费半」搬进 dispatcher 同一循环；**deque 原地保留**（跨轮批累积、`[INFER_PRESSURE]` 深度信号都还在）。

- [`StageAwareDispatcher`](../../app/services/inference/detection/dispatcher.py)：构造注入 `active_stages`/`stage_batch_sizes`/`submit_batch`/`capacity`；`_dispatch_loop` 每轮 `_fetch_and_dispatch_round()`（生产 → `_stage_queues`）后接 `_drain_and_submit()`（消费 → `proxy.submit`）。新增 `_pull_batch`（非阻塞拉 ≤N），删除带 timeout 自旋的 `get_batch_for_stage` 与 `queue_depth`。
- [`RemoteInferProxy.capacity()`](../../app/services/inference/detection/infer_proxy.py) 新增：`= max_inflight - inflight`（未就绪/停机返 0）。
- [`ModelWorkerService`](../../app/services/inference/detection/service.py)：proxy **先于** dispatcher 构造（要注入其 submit/capacity）；`start/stop` 去掉线程编排；删除 `_inference_loop`、`_stop_event`/`_worker_threads`、`BATCH_TIMEOUT_MS`，及随之变死的 `threading`/`time`/`guarded_run`/`AppError` import。

> **为什么单提交者能消掉「假丢帧」**：in-flight 只被 dispatcher `+1`、被 collector `-1`（collector 只会让额度**变多**）。故 dispatcher 每轮**先读一次 `capacity()`、再按额度逐批 submit**，对过量提交天然无竞态——取多少发多少，`submit` 正常路径不再返 False。原多提交线程会互相撞 `inflight` 满、把已取出的 batch 丢掉（`infer_inflight_full`）；现在只有停机/重启竞态才可能被拒。每轮**轮换起始 stage**，防额度被靠前 stage 长期吃满饿死后面。

## 保留项（刻意不动）

- **`detector.py` 保持独立**：它是被所有 workflow（clean/bending/bubble/mock）继承的**公共抽象**，不并入子进程 plumbing，守住 `__init__.py` 的「抽象 vs 管件」边界。
- **`MultiModelWorkerPool` 类名不改**：`MultiModel` 已表达「单 stage 内多模型」；模块名 `stage_pool` 补足「单 stage」语义即可。
- **deque `_stage_queues` + `[INFER_PRESSURE]` 日志**：保留，跨轮批累积与积压信号都在。
- **`MultiModelWorkerPool.infer_batch`（进程内组装路径）**：仍留作单测/回退。

## 前文表述修正

前一篇（进程隔离落地）反复用的不变式「**主进程 CUDA-free**」措辞不准，本次校正为「**主进程无 CUDA context**」：实时链路的时序 GRU（[`temporal/operator.py`](../../app/services/inference/temporal/operator.py)）一直在用 torch，只是**钉 `torch.device("cpu")`**、且惰性 import。真正的不变式是「主进程不建 CUDA context / 不做 GPU 前向」，不是「不 import torch」。沉淀 kb 时按此校正。

## 数据流（更新后）

```
主进程（单 dispatcher 线程）                     推理子进程（spawn，独占 GIL）
_dispatch_loop 每轮:
  ① _fetch_and_dispatch_round: pop ca_ready(捕获 cq) → _stage_queues[stage]
  ② _drain_and_submit:
       cap = proxy.capacity()                    _infer_models(frames, ts)
       轮换 stage、按 cap 拉批 → proxy.submit ──req_q──►  （单进程 FIFO 保序）
                                              ◄─resp_q── (req_id, merged)
  proxy._collect_loop: pending.pop(req_id) → _write_back_results（原样）
```

## 验证

| 项 | 结果 |
|----|------|
| 相关单测 | `test_infer_proxy` / `test_pool_ts_anchor` / `test_pipeline_drop_counters` / `test_inference_stage_routing` 全绿 |
| 顶层 torch-free | `import stage_pool` 后 `torch` 不在 `sys.modules` |
| 残留引用扫描 | `get_batch_for_stage`/`queue_depth`/`BATCH_TIMEOUT_MS`/`_inference_loop`/旧模块名 均清零 |
| 全量 `pytest tests/` | 355 passed |
