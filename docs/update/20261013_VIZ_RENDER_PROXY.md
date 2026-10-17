# 新增渲染子进程池代理 RemoteRenderProxy

> **变更状态**：生效中（2026-10-13）　组件已落地并测绿，尚无调用方（下一批接入可视化 worker）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)、[DESIGN_CONCURRENCY_AND_QUEUES.md](../kb/DESIGN_CONCURRENCY_AND_QUEUES.md)（2026-10-17）

## 概述

本批新增以下组件，只加组件与单测，不改调用点：

- [render_proxy.py](../../app/services/inference/online/visualization/render_proxy.py)：`RemoteRenderProxy`，负责共享内存槽、每键单飞、监督重启。
- [render_child.py](../../app/services/inference/online/visualization/render_child.py)：渲染子进程入口 `run_renderer` 和请求循环 `serve`。
- `SharedFrameRingView.write` 与 `SharedFrameRing.read`：支持请求-应答式用法，即结果原地写回同一槽位。
- `settings.viz_processes = 2`：渲染子进程数，下一批由 `VisualizationWorkerPool` 读取。

这是 15 路改造第 ⑤ 阶段的第 1 批，见 [20261013_SCALE_15_STREAMS_PROPOSAL.md](20261013_SCALE_15_STREAMS_PROPOSAL.md)。

## 变更背景

- **单线程渲染卡在 GIL 上，不是卡在渲染本身**：单帧渲染约 2.5ms，但开 1/2/4/8 个线程时总吞吐是 178/170/165/159 帧/s，不随线程数增长；旁边多一个忙线程，吞吐就降到 4 帧/s。
- **只有挪进子进程有效**：15 路合成负载下，渲染放进 2 个子进程能到每路 13.2–14.7fps，主进程渲染线程只有 2.5–10.7fps。

## 方案详情

### 全景

```text
viz 线程 ─► in_flight(task_id)? ─否─► submit(key, frame, specs, stage, events, ctx)
             │                          ├ 锁内：acquire(1) 槽位，记 pending / by_key
             │                          └ 锁外：帧拷进槽位 → req_q.put（请求只带槽位号、形状、RenderSpec）
             │
渲染子进程×P ─► req_q（P 个进程共享一条队列，自然负载均衡）─► read(slot) → FixedVisualizer.render
             │   → write(slot, annotated) → resp_q.put((req_id, shape, render_s, ok))
             │
viz 线程 ─► drain()：非阻塞取走答复 → 读槽位结果 → 归还槽位 → 清该键在途 → 返回 (ctx, annotated, render_s, ok)
监督线程 ─► 子进程死亡 / 最老在途 >2s 未答 / 久不就绪 → kill 全部子进程、清在途（计 viz_child_down）、reset 环、重建队列、退避重启
```

### 设计约束

- **每键单飞**：同一 run 同时最多一个在途渲染。processed 轨要求帧按 ts 顺序进入 `ca_processed`，多进程并发渲染同一 run 会乱序。
- **故障时全部重启，而不是只重启出问题的那个**：P 个子进程共享一条请求队列，判不出卡住的请求在哪个子进程上；而且被杀的子进程可能持着队列的读锁，所以按整组 kill 并重建队列处理，做法同推理代理。
- **渲染失败的兜底**：子进程渲染失败时不写槽位，答复 `ok=False`。主进程照样读槽位，拿到的是未标注的原始帧，与旧实现「渲染异常返回 `frame.copy()`」语义一致。
- **子进程的 import 链不得拉起 torch**：用导入门禁锁住，`render_child` 不带重依赖，`visualizer` 只许 cv2。
- **子进程忽略 SIGINT 和 SIGBREAK（Windows）**：Ctrl-C / Ctrl-Break 会广播给整个进程组。不忽略的话，子进程会先于父进程的有序停机死掉，监督线程误判为失败，打 ERROR 并安排重启。停机统一由父进程 `terminate` 驱动，这是 2026-10-13 后端起停冒烟时发现后补上的。
- **`drain` 由调用方线程拉取，代理不设收集线程**：完成结果在 viz 线程上写回，吞吐统计字典仍然只有一个线程访问，不需要额外加锁。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_render_proxy.py`（单飞、读槽位并归还、槽满 / 帧放不进 / 未就绪拒收、put 失败归还、孤儿答复丢弃、失败清理并计丢帧；`serve` 原地渲染、渲染失败保留原帧） | 9 passed |
| 导入门禁：`render_child` 无重依赖、`visualizer` 只许 cv2 | passed |
| 真 spawn 冒烟（本地，2 个子进程，640×480）：15 个键各 20 帧，300/300 全部回收且每键有序，约 625 帧/s；kill 一个子进程后 2.4s 内重启就绪；停机干净 | 通过 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 处理计划 |
|------------|---------|
| 尚无调用方 | 下一批接入 `VisualizationWorker` |
