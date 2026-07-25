# CQ 生命周期与拆除编排（T1–T3）

> **变更状态**：实施工单（2026-07-01 修订）；T1–T3 已落地
> **知识库**：已沉淀 → [kb/SERVICE_CLIENT_STATE.md](../kb/SERVICE_CLIENT_STATE.md)(2026-07-21)

## 工单定位

把 CQ 从「跨 run 复用的可变对象」改成「一 run 一实例的不可变 run 对象」，加状态机，并把散落两处的拆除逻辑收成单一编排出口。承载 **T1 / T2 / T3**。全程仍按现有 client_id 键落地（换键属后续工单），三步独立可绿。

## 运行身份契约（本文自足）

概念：**task** = 业务任务（`clean_task.task_id`）；**step** = 清洗阶段（`int(current_step)`），定 stage 路由 + 存储分区；**run** = 一次活跃运行，由**一个 CQ 实例**代表，终生绑定一个 `(task_id, step_id)`、不换 step；**source_ip** = 被动字段（旧 client_id，流来源/诊断），非路由键、不承担互斥。

本工单据此落三条不变式：

- **CQ 实例 == 一次 run == 一个 `(task_id, step_id)`**：构造注入不可变身份，不再原地复用；
- 判别 run 实例 = **CQ 对象引用**（不引入 `run_epoch`，也不让消息携带身份）；
- 两道栅栏：`(task,step)`/对象身份隔离跨 run；状态机隔离同 run 拆除期；
- 重启 = supersede（新 run 覆盖/截断该 `(task,step)` 分区，不引入 `{task}/{step}/{run}` 存储层级）。

### 现状根因

[`InferenceManager._set_task_locked`](../../app/services/inference/manager.py) 切 step 时不换 CQ，而在同一 CQ 上原地改 `task` + `clear_task_caches()` + 换 actor。[`queues.py`](../../app/services/client/queues.py) 里 `set_task`/`clear_task_caches`/`set_stage`/`clear()` 的「重置成 MOCK 再复用」整套机器，都是为「一个 CQ 服务多个 run」存在——T1 拆除它。

---

## T1 — CQ per-run 不可变化（下线原地复用）

**依赖**：无（序列起点）　**量**：L（2–3d）　**风险**：中高（创建/切换语义变）

范围：

- CQ 构造接收不可变 `(task_id, step_id, source_ip)` + 固定 `stage`（由 step 经配置纯函数解析一次）；
- **删除** [`queues.py`](../../app/services/client/queues.py) 的 `set_task` / `clear_task_caches` / `set_stage`、`clear()` 的复用重置分支、`_resolve_step_id` 对活态 `current_step` 的解析；
- `task`（`Optional[CleaningTask]`）收敛为「不可变身份 + 仅 `status` 可变」；`append_ca_raw/processed` 直接读 `self.step_id`；
- [`InferenceManager.set_task`](../../app/services/inference/manager.py) → `start_run`：每 run 建**新** CQ，注册表槽位**替换而非原地改**；切 step/重启 = 建新 CQ + 拆旧 CQ；
- decoder（[`FFmpegDecoder`](../../app/services/stream/decoder.py)）、actor（[`temporal/actor.py`](../../app/services/inference/temporal/actor.py)）构造时接收所属 CQ，step_id 从 CQ 读，不反查；
- FeatureStore/FactLedger 对新 run **open-fresh/截断**该 `(task,step)` 分区（重启覆盖）。

**ship-green**：创建/切换语义变、对外接口不变；仍 client_id 键。
**验收**：连续重启同 `(task,step)` 存储无新旧混写；切 step 期间旧 settlement 归属旧分区。

## T2 — CQ 状态机 + 写入门 + close()

**依赖**：T1　**量**：L（2–3d）　**风险**：中（并发，不可误丢合法尾部）

状态与允许行为：

| 操作 | ACTIVE | DRAINING | CLOSED |
|------|--------|----------|--------|
| Decoder 写 ca_ready/raw | ✓ | ✗ | ✗ |
| 推理结果写回 | ✓ | ✗ | ✗ |
| Actor 实时 tick | ✓ | 停 | ✗ |
| settlement 告警 | ✓ | ✓ | ✗ |
| raw/processed flush 落 HLS | ✓ | ✓ | 已完成 |
| 前端读快照 | ✓ | 空 | 空 |

范围：

- 加状态枚举 + 幂等转换；各写法（`append_ca_*`、`set_latest_inference`、`push_detection`、`append_alarm_record_with_gate`、`set_latest_rendered`）按表门控；
- `close()` 从「重置复用」改为「**释放重数据留壳**」（清 frames/滑窗/告警/快照，留不可写小对象）；
- 迟到写入靠**写入时刻**查 state 拒绝（不是 dispatch 时刻）；配合 T4 的句柄写回，late result 落到 DRAINING/CLOSED 旧 CQ 被挡。

**ship-green**：仍 client_id 键；只加门与状态。
**验收**：DRAINING/CLOSED 拒写计数；超时 join 后旧线程写入被拒；覆盖 late result / actor timeout / 并发 restart。

## T3 — `_teardown_run` 单一出口 + 编排收敛

**依赖**：T2　**量**：M/L（2–3d）　**风险**：中（跨 service 编排 + 锁模型）

范围：

- 在**控制面**（薄模块 `app/services/run_lifecycle.py`，api router 与 HealthMonitor 都 import，避免 service 反 import router）实现单一 `_teardown_run(task_id, expected_cq)`：

  ```text
  若 registry[task_id] is not expected_cq: no-op   # 对象身份 fence
  cq → DRAINING
  StreamService.stop_stream(task_id)               # 停 decoder（owner=Stream）
  actor.finalize_and_stop()                        # 停 actor + settlement（owner=Inference）
  flush 残余 HLS / feature.close
  cq.close()
  registry.remove(task_id, expected=cq)
  ```

- **合并**今天重复的 [`api._manual_cleanup_fallback`](../../app/routers/api.py) 与 `HealthMonitor.cleanup_client` 两份拆除步骤为此一处；
- **锁模型**：per-task `threading.Lock`（不再 `asyncio.Lock`），api async 端点经 `asyncio.to_thread` 调用，HealthMonitor 线程直接调，共用同锁；收编 [`InferenceManager._client_lifecycle_lock`](../../app/services/inference/manager.py)；
- 三调用者接入：`terminate`、`start` 重启、**HealthMonitor 重连耗尽自动结束**（纯运行态回收，不发 alarm、不写 DB 终态）；
- HealthMonitor 保留 reconnect/孤儿回收/资源兜底大脑，只把「拆活 run 的步骤」委托给此出口。

**ship-green**：仍 client_id 键（`_teardown_run(client_id)` 形态，T5 再改 task_id）；行为等价但收敛为一处、一把锁。
**验收**：并发 terminate/restart/自动结束不双拆、不误删；对象身份 fence 使迟到 cleanup 变 no-op；HealthMonitor reconnect 成功路径不受影响。

---

## 内存回收边界

- 确定性 payload 释放：`close()` 清帧队列/滑窗/告警/快照；
- 对象最终释放：出注册表 + decoder/actor 释放最后强引用后 GC；
- 保证：旧线程超时未退时，旧 CQ 只剩不可写小壳，不再持大块 numpy 帧。

## 非目标

- 不换 ClientManager 键（T5）；不改写写回路径（T4）；
- 不改 HTTP/DB/wire；
- 不引入 `run_epoch` 或消息携带身份；
- 不引入 `{task}/{step}/{run}` 存储层级；
- 不要求无限等待 CUDA/Decoder 线程物理退出。

## 完成判据

- CQ 一 run 一实例、身份不可变；`set_task/clear_task_caches/set_stage` 复用重置移除；
- 迟到写入被状态机拒；close 后不持重数据；重启 supersede 不污染存储；
- 拆除只有一个 `_teardown_run` 出口，per-task threading 锁串起 terminate/restart/自动结束；
- 全量 `pytest tests/` 通过，覆盖并发 restart / late result / actor timeout。
