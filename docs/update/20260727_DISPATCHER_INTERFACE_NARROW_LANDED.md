# 收窄 dispatcher↔proxy 接口：capacity 内化 + peek-commit 轮转排空（生效中）

> **变更状态**：生效中（2026-07-27）。落地 [20260727_DISPATCHER_ALLOC_BACKPRESSURE_PLAN.md](20260727_DISPATCHER_ALLOC_BACKPRESSURE_PLAN.md) 的「①（含职责边界修正）」；「③ 入口降帧」仍属过载区、未落地（只留接缝）。行为在稳态（在途额度充足）下与前一版等价，全量 `pytest tests/` 361 passed。
> **知识库**：待沉淀 → `kb/SERVICE_INFERENCE.md`（dispatcher↔proxy 接口从 `(submit, capacity)` 收窄为 `submit` 布尔背压；`_drain_and_submit` 改 peek-commit）。
>
> 承接：建立在「单提交者」模型（[20260726_DETECTION_REPACKAGE_SUBMIT_MERGE.md](20260726_DETECTION_REPACKAGE_SUBMIT_MERGE.md)）之上——那次让 dispatcher 成为唯一提交者、并临时保留 `capacity()` 供「先读额度再发」。本次删掉该临时信号：单提交者下它已冗余。

## 改了什么

1. **`infer_proxy.py` 删 `capacity()`**：单提交者下它只把 proxy 私有的 `inflight` 泄漏给 dispatcher。请求限流本就是 proxy 固有职责——`submit()` 早已返回布尔（`inflight >= max_inflight` 即 False），无需外泄计数。
2. **`dispatcher.py` `_drain_and_submit` 改 peek-commit 轮转排空**：不再预读 cap。外层 `while` 转圈、每圈每 stage `_peek_batch`（切片看、不移除）→ `submit`，接了才 `_commit_pop`（popleft），被拒即 `return`（帧原封留 deque、背压沿链上传）；整圈无进展则停。显式实现「每 stage 每圈一批」的公平分配（修掉预案里的缺口 B 锯齿）。删除旧 `_pull_batch` 与 `infer_inflight_full` 埋点（peek 未取出，被拒是正常背压、不再是「已 popleft 才发现满」的假丢帧）。
3. **`service.py` 去掉 `capacity=self._proxy.capacity` 注入**：dispatcher 构造只留 `submit_batch`。
4. **背压反馈接缝（只留挂载点，未实现策略）**：`_fetch_and_dispatch_round` 在 append 前加透明准入 `_admit_to_stage(stage)`（**本次恒 True**）；预留字段 `_stage_backpressure`（约定 drain 侧写、admit 侧读的单向异步通道，本次不接通）。供未来「入口降帧」把 drain 侧下游压力回传取帧侧，无需再动结构。

## 契约澄清（本次刻意钉死、未改代码）

- **`_write_back_results` 是 proxy→service 的注入式写回回调**，非 proxy 直调 service：service 构造时把它作 `write_back=` 交给 proxy；proxy 的 **collector 守护线程** 据 req_id 重组 `List[FrameInference]` 后回调它。职责切分——proxy 只「重组 + 回调」，不知 FeatureStore/cq/stale_run；回调实现独家承担 `is_active()` 迟到门 + 三写 + FeatureStore.append。它与被删的 `capacity` 是**方向相反的两条独立契约**（推 vs 拉），本次只动「拉」向。
- **fetch/drain 顺序执行不互相阻塞**：同一 `_dispatch_loop` 线程内先后调用，`_drain` 撞限流 `return` 立即结束本轮、不阻塞下轮 `_fetch`；唯一共享态 `_stage_queues` 为同锁短临界区，无跨阶段持锁。fetch↔drain 的耦合方向被显式定为**单向、跨轮异步**（drain 本轮沉淀 → 下轮 fetch 读），避免未来把背压塞进 drain 同步路径反噬取帧。

## 背压语义（表述校正）

不是「把背压收进 proxy」。请求限流（`inflight` 上限 + `submit` 拒收）**本就是 proxy 固有职责**；背压是一条**沿链上传的传播**：proxy 限流（`submit` False）→ dispatcher 帧留 deque → deque 积压/`infer_backlog` 淘汰 → 上游 `ca_ready`/入口。本次去掉的是 dispatcher **预读 cap 抢先分配**这个多余间接层，改为撞到限流才响应并向上传播。

## 验证

| 项 | 结果 |
|----|------|
| 新增 `tests/test_dispatcher_round_robin.py` | 轮转均衡 / 被拒帧留 deque 不丢 / 稳态每 stage 一批 / admit 透明，4 用例全绿 |
| 相关单测 | `test_infer_proxy` / `test_pipeline_drop_counters` / `test_inference_stage_routing` 全绿 |
| 全量 `pytest tests/` | 361 passed |
| 残留引用扫描 | `.capacity` / `_pull_batch` / `infer_inflight_full` / `self._capacity` 在 `app/services/inference/detection/` 与 `tests/` 均清零 |

## 未落地（留待观测触发）

- **③ 入口按 ts 主动降帧**：本次只留 `_admit_to_stage` + `_stage_backpressure` 接缝，策略未实现。待 `[INFER_PRESSURE]` 观测到成因 B（inflight 长期贴 max、所有 stage 一起涨）再接通 drain→fetch 反馈。
