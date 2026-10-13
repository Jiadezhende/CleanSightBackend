# 可视化渲染挪进子进程：15 路 processed 出帧从每路 6–6.7fps 提到 12.6–13.2fps，主进程 CPU 减半

> **变更状态**：生效中（2026-10-13）
> **知识库**：待沉淀

## 概述

`VisualizationWorker` 接入上一批新增的 `RemoteRenderProxy`（[20261013_VIZ_RENDER_PROXY.md](20261013_VIZ_RENDER_PROXY.md)），渲染从 viz 线程挪进子进程：

- **主进程**：viz 线程仍负责轮询、去重、生成 `RenderSpec`（各检测器的绘制指令）、写回和吞吐统计；渲染交给 `settings.viz_processes`（默认 2）个子进程，结果在下一个 tick 收回。
- **就地渲染兜底**：子进程池未就绪、槽位满或帧放不进槽位时，在本线程就地渲染，即旧路径。
- **关闭开关**：`viz_processes = 0` 时全部在本线程渲染，供开发机和少路数场景使用。

前后端契约不变：processed 轨 HLS、`WS /ai/video` 的 JPEG 帧都与改前一致。这是 15 路改造第 ⑤ 阶段的第 2 批，见 [20261013_SCALE_15_STREAMS_PROPOSAL.md](20261013_SCALE_15_STREAMS_PROPOSAL.md)。

## 变更背景

- **单线程渲染的瓶颈是 GIL**：15 路时单线程渲染每路只能出约 6fps，processed 轨和实时画面都会断帧。
- **已定参数**：2026-10-13 与负责人确认，渲染子进程数取 2。

## 方案详情

### 全景：一个 tick 内的顺序

```text
① drain()：收回子进程已完成的渲染 → _write_rendered（ca_processed + latest_rendered，非 ACTIVE 的 cq 由写门拒收）
② 遍历活跃 run：
   有新推理结果？──否──► 空转计数
   └是─► 该 run 有在途渲染？──是──► 跳过（等结果回来再送最新的）
         └否─► 生成 RenderSpec → submit 给子进程池
                ├ 收下：去重时间戳此刻推进，结果在下一 tick 的 ① 写回
                └ 被拒（未就绪 / 槽满 / 帧放不进 / 该 stage 无检测器）→ 本线程就地渲染并写回（旧路径）
```

| 部件 | 落在哪 |
|------|--------|
| tick 开头 drain、单飞检查、提交或就地渲染兜底；抽出 `_build_specs` / `_write_rendered` / `_note_render_time` | [visualization_worker.py](../../app/services/inference/online/visualization/visualization_worker.py) |
| 池生命周期：`VisualizationWorkerPool.start` 建并启动代理，起不来则记 error 并退回线程内渲染；`stop` 先停线程再停代理 | 同上 |
| 装配参数：`render_processes = settings.viz_processes`；`frame_bytes` 按 CQ 声明的帧尺寸算；`render_slots = max(32, 2 × capacity_planned_streams)` | [online/service.py](../../app/services/inference/online/service.py) |

### 约束

- **每个 run 同时只有一个在途渲染**：所以就地渲染兜底和子进程渲染交替出现时，processed 帧仍按 ts 顺序进入 `ca_processed`。
- **render-bound 判据的口径**：子进程渲染时，`[VIZ_THROUGHPUT]` 的渲染耗时取子进程实测值。viz-starved 和 supply-bound 的判据不变。
- **停机顺序**：先停 viz 线程，再停代理，在途渲染计入 `viz_child_down`。停机时各 run 已由 `run_control` 先拆除，cq 已关闭，这些结果本来也会被写门拒收。

## 变更效果

测试负载用真实组件 15 路：`FFmpegDecoder`（文件输入，`-re` 30fps，640×480）+ 真实 `ClientQueues` + 模拟推理（每帧 3+3 个框，15fps 写检测快照）+ 真实 `VisualizationWorkerPool`。本地运行，进程锁在 P 核，每组 12s：

| 渲染子进程数 | processed 出帧 fps/路 平均（最低） | 主进程 CPU |
|------|------|------|
| 0（线程内，旧路径） | 6.0 (6.0) / 6.7 (6.7) | 1.57 / 1.58 核 |
| **2（默认）** | **12.6 (10.7) / 13.2 (11.3)** | **0.73 / 0.86 核** |
| 3 | 13.8 (13.3) | 0.74 核 |
| 4 | 13.8 (13.4) | 0.81 核 |

> 2 个子进程时，每路平均比 3–4 个低约 1fps，最低值低约 2–3fps。差距来自子进程排队：每秒 225 次渲染分给 2 个进程，排队时间变长，期间新的推理结果把旧的覆盖掉，帧就被跳过。3–4 个子进程也只到 13.8fps，仍低于第 ⑥ 阶段的 14fps 达标线，所以加进程不是默认解法；达标线保持 14fps（2026-10-13 与负责人确认），不达标时按提案 §6 的 `[VIZ_THROUGHPUT]` 归因处理。

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_viz_worker_render_proxy.py`（新增：提交后在下一 tick 收回写回且不重送、在途 run 不重送也不就地渲染、池拒收时就地渲染、已关闭 run 的迟到结果被写门拒收） | 4 passed |
| `tests/test_viz_throughput_snapshot.py`（未改，线程内路径） | passed |
| 全量 `pytest tests/` | 1035 passed |
| 后端起停冒烟（本地 dev，uvicorn + `config/logging.json`）：6s 就绪，推理与渲染两个代理都带共享内存环启动（日志 `ring=32x921600B`）；Ctrl-Break 优雅停机 2.5s 完成，两个代理正常 stopped、在途为 0，4 个子进程全部退出、无误报重启 | 通过 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 2 个子进程时每路平均 12.6–13.2fps，低于提案达标线 14fps | processed 轨与实时画面偶有跳帧 | 第 ⑥ 阶段验收时看 `[VIZ_THROUGHPUT]` 归因：render-bound 才调大 `CLEANSIGHT_VIZ_PROCESSES`，supply-bound 查推理吞吐 |
| 基准只在 Windows 笔记本上测过，推理是模拟的 | 绝对值与生产不同 | 以测试环境 15 路压测为准 |
| `ClientQueues.get_latest_frame()` 每帧整帧 copy，随后又拷进共享内存槽 | 主进程每帧多一次约 0.1ms 的拷贝 | 不影响达标，暂不处理 |
