> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Client State Service

客户端状态服务围绕 `ClientManager` 和 `ClientQueues` 展开，是流、推理、可视化、告警、追溯落盘之间的共享状态层。

**运行键 = `task_id`(int)**：注册表、per-task 锁、decoder、Actor、落盘分区全链路统一用 int `task_id`。`source_ip` 降级为**被动来源字段**（诊断 + 遗留 wire 适配），不再是路由键。跨服务的起停编排不在本层，归 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## ClientManager（COW 注册表）

`ClientManager` 是「读多写少」的中台：注册表是一本**不可变 dict** `_runs: {task_id → ClientQueues}`，读者原子读引用后免锁迭代；写者在 `_wlock` 下复制—改—换引用（COW）。本类只做哑存储，**不构造 CQ**（CQ 由 RunController 建好后 `set` 换槽）。

- 读接口（无锁）：`get(task_id)` O(1) 直取、`has_client`、`snapshot()`（零拷贝只读视图）、`get_all_queue_depths`、`get_client_count`、`get_status_summary`。
- 写接口（`_wlock` + COW）：`set(task_id, cq)` 换槽、`remove`、`remove_if`、`clear_all`。
- `find_by_source_ip(source_ip)`：**遗留 wire 适配垫片**——前端 `/terminate`、WS `/ai/video` 仍以 `?client_id=<source_ip>` 调用，扫描快照匹配首个命中回解成当前 run。前端换键完成后可删。

### 两把锁分工

- `_wlock`：全局极短，只护 COW 换引用（create/remove）。
- `lock_for(task_id)` → per-task `RLock`（生命周期事务锁）：串行化同一 task 的 start/stop/restart。由 RunController 与 HealthMonitor 共用，消除「HM 迟到 cleanup 误删 /start 刚建 CQ」的竞态。RLock 可重入（start_run 持锁内再调 stop_run 不自死锁）。

### 对象身份 fence

`remove_if(task_id, expected_cq)`：仅当 `registry[task_id] is expected_cq` 才删除。防止迟到 cleanup 误删「同键新实例」（重启/切换后装入的新 CQ）。

## ClientQueues：一次 run 的不可变身份 + 状态机

**一个 CQ == 一次 run == 一个 `(task_id, step_id)`**。身份 `task_id/step_id/source_ip/stage` 为构造定死的不可变 primitive，热路径免锁直读；切 step/重启 = 建**新** CQ 换槽，不在旧对象上改身份（故 settlement 归属天然正确，无「先停旧 actor 再切字段」的排序不变式）。已删除旧的 `set_task/current_step/status` 可变字段与 `task_id→client_id` 映射。

### 运行状态机 `RunState`（单调 ACTIVE→DRAINING→CLOSED）

- **ACTIVE**：正常运行，所有写放行。
- **DRAINING**：拆除中，封生产者写（decoder 抽帧 / 结果写回 / tick），仍放行 settlement 告警 + HLS flush。
- **CLOSED**：payload 已释放，一切写被拒；不可变身份小壳仍可读（供 fence/日志）。

门控在**写入时刻**判 state（非 dispatch 时刻）：迟到写落到 DRAINING/CLOSED 的旧 CQ 被拒，不串台到新 run。状态读免锁（枚举原子读 + 单调），`_state_lock` 只串行转换本身，绝不与 payload 锁互嵌。转换入口：`to_draining()`（幂等）、`close()`（置 CLOSED + 释放 payload，`clear()` 为其兼容别名）。

写门非对称示例：`append_alarm_record_with_gate` 仅 CLOSED 拒（DRAINING 放行，保 settlement 入账）；`set_latest_rendered/set_latest_temporal` 拒**非空**写但放行清空（拆除期清前端残帧）。

### 队列与槽位

- `ca_ready`：待推理帧，**无锁 SPSC deque**（单生产者 decoder / 单消费者 dispatcher）。入队走 `append_ca_ready_with_throttle`（Bresenham 相位累加器均匀抽帧 `inference_fps/raw_fps` + 背压），非 wall-clock。
- `ca_raw` / `ca_processed`：raw / processed HLS 落盘**纯缓冲**（`_raw_lock` / `_viz_lock`）。**不触发落盘**——persistence 的 HLSSegmentSweeper 周期 `take_raw_segment()` / `take_processed_segment()` 主动**拉取**（PULL）。
- `_latest_rendered`：最新渲染帧单槽，供 WebSocket 实时推流（前端轮询）。
- `_latest_inference`：最新推理结果原子快照，供 VisualizationWorker。
- `_slide_window`：per-stream(detector.name) 检测环形缓冲，供时序分析。保留时长 = `max(10s 底线, 该流感受野)`，感受野经 `set_stream_windows` 由 InferenceManager 配置、只向上扩展（signals_10s 的 10s 不受影响）。
- `_latest_temporal`：最新时序事件，供前端 overlay 与消息接口。
- `_alarm_log`：内存告警环形日志（maxlen 100），供 `/task/message/{task_id}`。

## 告警 Gate

`append_alarm_record_with_gate(alarm, mode)` 单 `_alarm_lock` 内原子完成闸门去重 + 赋 seq + 入日志。闸门按 `(self.task_id, alarm.metric, mode)` 做固定 5 秒冷却；`task_id` 取自 CQ 不可变身份，无需调用方传入。被拦截的告警不入账。

## 前端消息

`/task/message/{task_id}` 经 `client_manager.get(task_id)` 找活跃 CQ，`get_alarm_snapshot(since_seq)` 原子返回告警增量 + max_seq，并附 `signals_10s`。CQ 只吐自有词汇的原始数据（流名聚合），流名→metric 的展示映射归 router 装配层，不下沉本层。

## 代码来源

- `app/services/client/manager.py`
- `app/services/client/queues.py`
- `app/services/client/config.py`
- `app/services/run_control.py`
- `app/routers/task.py`
- `tests/test_alarm_increment.py`
- `tests/test_task_message_api.py`
- `tests/test_teardown_identity_fence.py`
