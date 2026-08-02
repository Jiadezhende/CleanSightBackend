# 推理子进程「活着但没就绪」不再是不可恢复态（补收 + 判失败）

> **变更状态**：生效中（2026-07-28）。修的是一个**尚未在现网触发**的静默失效路径。
> **知识库**：已沉淀 → [SERVICE_INFERENCE](../kb/SERVICE_INFERENCE.md)（RemoteInferProxy 监督第三判据 _check_not_ready）(2026-08-02)
> **前置**：[20260726_INFER_PROCESS_ISOLATION_LANDED.md](20260726_INFER_PROCESS_ISOLATION_LANDED.md)。

## 缺陷

`_spawn_child` 只 `ready_ev.wait(ready_timeout=120s)` **一次**，超时即返回且**不置** `_child_ready`。
此后：

| 监督判据 | 该场景下 | 结论 |
|---|---|---|
| `dead`（进程死） | 子进程活着（模型仍在加载 / warmup 卡在 CUDA） | 不成立 |
| `wedged`（在途 >0 且久无响应） | `submit` 因 `not _child_ready` 全被拒 → `inflight` 恒 0 | 不成立 |

两条判据同时哑火 → **永不重启，全链路 0 推理**。而且子进程哪怕在第 121 秒真的 ready 了也没人再看一眼，那一刻的 `ready_ev.set()` 落进虚空。外部唯一可见的症状是 dispatcher 的 `[PRESSURE] resource=stage_queue … reject_total` 在涨、`frame_drop_total{reason=infer_backlog}` 在涨——**没有任何一行日志说"推理不可用"**。

触发条件：warmup 超过 120s（冷盘首次加载大权重、GPU 被别的进程占着、驱动 hang）。

## 修法：两步，都在监督线程里

新增 `_check_not_ready(dead)`，每 tick 跑一次（`_supervise_loop` 里与 dead/wedged 并列）：

1. **补收迟到的就绪信号** —— 子进程只是慢、屏障超时后才 `ready_ev.set()`：补看一眼就置 `_child_ready` 恢复接收。**不杀了重来**，重来还要再付一次模型加载。打一条 WARNING 留痕。
2. **仍不就绪 → 判失败** —— `now - spawn_at > ready_timeout`，交给既有失败路径 `_handle_child_failure(not_ready=True)`：kill → 清在途 pending（计 `infer_child_restart`）→ 退避重 spawn。

于是「静默不可用」变成「有限次重试 + 每轮一条 ERROR」，且模型文件补回/GPU 让出后**自动恢复**，不需人工重启后端。

**为什么不设「再宽限几秒」的缓冲带**（评审时删掉的一版）：当初的理由是「`_spawn_child` 自己也在 `ready_timeout` 处到点返回，监督线程可能杀在它刚要置 ready 的瞬间」。这理由站不住——补收读的**就是** `_spawn_child` 正在 wait 的同一个 `ready_ev`，该竞态本就由步骤 1 挡住；多让几秒只是把同一个亚微秒窗口整体后移，不消除它，代价却是每次真失败都多躺那几秒。判据用 `ready_timeout` 单一时间源，不叠第二个常量。

## 未做（有意）

- **不给 `max_restarts` 设上限**：默认仍 `None`（无限重启，退避封顶 30s）。不可修复的故障（权重文件缺失）确实会一直重试，但每轮一条 ERROR，且运维修好后自动恢复——比"重试 5 次后彻底躺平、必须重启进程"更符合常驻服务的预期。
- **不把 proxy 就绪状态接进 `/health/status`**：当前的可观测性下限是每 10s 一条 `[PRESSURE] … reject_total` + 每次失败一条 ERROR，够定位。真要做成"推理不可用"的一等健康信号（供告警系统消费）是另一个动作，届时一并考虑 dispatcher 侧的表达。

## 验证

`tests/test_infer_proxy.py` 新增 4 例（迟到就绪被补收且不重启 / 活着久不就绪判失败 / ready_timeout 内不误判 / 已就绪的子进程永不被该判据碰）。全套 391 passed。
