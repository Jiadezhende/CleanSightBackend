# 消息上下文传播任务 —— 已被 2026-07-01 方案吸收（SUPERSEDED）

> **变更状态**：superseded（2026-07-01）　<!-- 本工单的问题被更根本的方案消解，不再单独实施 -->
> **知识库**：无需沉淀
>
> 去向：[20260628_RUNTIME_IDENTITY_BINDING_TASK.md](20260628_RUNTIME_IDENTITY_BINDING_TASK.md)（契约）、[20260628_CLIENT_QUEUES_LIFECYCLE_TASK.md](20260628_CLIENT_QUEUES_LIFECYCLE_TASK.md)（T1 根治）、[20260628_CLIENT_ROUTING_BOUNDARY_TASK.md](20260628_CLIENT_ROUTING_BOUNDARY_TASK.md)（T4 写回句柄化）。

## 为什么被吸收

本工单原目标：让「消息产生后仍需反查当前任务才能被理解」消失——办法是给 `Frame`/`DetectionTask`/`FrameInference` 焊上不可变 `RunIdentity`，下游只读消息自带身份。

后续设计发现这套「消息携带身份」是在**绕过**真正的病根，而非治它：

- 活态反查（`cq.get_task_id()` 等）之所以危险，是因为**CQ 可变、跨 run 复用**——`set_task` 原地改 `task`，反查可能读到已切换的值；
- 一旦 CQ 改成 **per-run 不可变**（见 [CQ 生命周期 T1](20260628_CLIENT_QUEUES_LIFECYCLE_TASK.md)），从 CQ 句柄读 `cq.step_id` 就完全安全，反查隐患自然消失；
- 唯一真正的异步串台风险（推理迟到写回）用 **写回拎捕获的 CQ 句柄 + 状态机拦截**解决（见 [换键与路由 T4](20260628_CLIENT_ROUTING_BOUNDARY_TASK.md)），不需要在每条消息上带身份，也不需要 `run_epoch`。

因此「消息携带 RunIdentity + run_epoch」整套被删；判别 run 实例改用 **CQ 对象引用本身**。原工单不再单独实施，其关切分别落在 T1（不可变 CQ 根治反查）与 T4（句柄写回封串台）。

## 历史动机（保留备查）

原始问题清单——`Frame` 缺运行身份、`DetectionTask/FrameInference` 重复散参、落盘前从活态 CQ 补身份、Dispatcher 从活态 stage 构造任务——均已在上述两份工单中以「不可变 CQ + 句柄」方式覆盖，不再逐条携带身份解决。
