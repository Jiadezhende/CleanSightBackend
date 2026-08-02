> 更新时间：2026-08-02
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

**一个 CQ == 一次 run == 一个 `(task_id, step_id)`**。身份 `task_id/step_id/source_ip/stage` 为构造定死的不可变 primitive，热路径免锁直读；切 step/重启 = 建**新** CQ 换槽，不在旧对象上改身份（故 settlement 归属天然正确，无「先停旧 actor 再切字段」的排序不变式）；身份无可变 setter。

### 运行状态机 `RunState`（单调 ACTIVE→DRAINING→CLOSED）

- **ACTIVE**：正常运行，所有写放行。
- **DRAINING**：拆除中，封生产者写（decoder 抽帧 / 结果写回 / tick），仍放行 settlement 告警 + HLS flush。
- **CLOSED**：payload 已释放，一切写被拒；不可变身份小壳仍可读（供 fence/日志）。

门控在**写入时刻**判 state（非 dispatch 时刻）：迟到写落到 DRAINING/CLOSED 的旧 CQ 被拒，不串台到新 run。状态读免锁（枚举原子读 + 单调），`_state_lock` 只串行转换本身，绝不与 payload 锁互嵌。转换入口：`to_draining()`（幂等）、`close()`（置 CLOSED + 释放 payload，`clear()` 为其兼容别名）。

写门非对称示例：`append_alarm_record_with_gate` 仅 CLOSED 拒（DRAINING 放行，保 settlement 入账）；`set_latest_rendered/set_latest_temporal` 拒**非空**写但放行清空（拆除期清前端残帧）。

### 队列与槽位

- `ca_ready`：待推理帧，**无锁 SPSC deque**（单生产者 decoder / 单消费者 dispatcher）。入队走 `append_ca_ready_with_throttle`（整数降采样"每 N 帧留 1"，N=`inference_decimation`，`_decimate_counter` 计数 + 背压），非 wall-clock。
- `ca_raw` / `ca_processed`：raw / processed HLS 落盘**纯缓冲**（`_raw_lock` / `_viz_lock`）。**不触发落盘**——persistence 的 HLSSegmentSweeper 周期 `take_raw_segment()` / `take_processed_segment()` 主动**拉取**（PULL）。三条 CA 队列（`ca_ready`/`ca_raw`/`ca_processed`）共享容量 `ca_maxlen`，由 `settings.ca_maxlen_seconds`(默认 **30s**) × `settings.raw_fps`(30) 派生 = **900 帧**（时间为跨子系统货币，帧数换算在 `ClientConfig.ca_maxlen` 属性边界）。`ClientQueues.__init__` 裸建默认 `ca_maxlen=900` 是仅裸建/测试兜底的第二真源，生产路径恒被 `cq_kwargs()` 覆盖。取 30s 的取舍：推理腿 `ca_ready` 被 dispatcher 恒掏空、天花板只兜底；录制腿 `ca_raw` 才需余量（丢帧=录像永久空洞），30s≈3 个 HLS 段吸收分段消费抖动。
- `_latest_rendered`：最新渲染帧单槽，供 WebSocket 实时推流（前端轮询）。
- `_latest_inference`：最新推理结果原子快照，供 VisualizationWorker。
- `_slide_window`：per-stream(detector.name) 检测环形缓冲，供时序分析。保留时长 = `max(10s 底线, 该流感受野)`，感受野经 `set_stream_windows` 由 InferenceManager 配置、只向上扩展（signals_10s 的 10s 不受影响）。
- `_latest_temporal`：最新时序事件，供前端 overlay 与消息接口。
- `_alarm_log`：内存告警环形日志（maxlen 100），供 `/task/message/{task_id}`。

### CA 队列配置校验（关系式，非绝对帧数）

`ClientConfig._validate_config`（`services/client/config.py`）只查**关系不变式**、不查绝对帧数——本模块货币是时间，绝对帧阈值语义会随 `raw_fps` 漂移。原 `if ca_maxlen < 300` 地板已删（秒制重构前遗留，其值与 `ca_segment_len` 相等纯属巧合）。现三条判据，阈值提为具名常量：

| # | 判据 | 级别 | 理由 |
|---|---|---|---|
| 1 | `ca_segment_seconds < _MIN_SEGMENT_SECONDS`(5.0)，**按秒判** | ⚠️ warning | 段过短 → 段数与每段 ffmpeg 固定开销放大；与 raw_fps 无关 |
| 2 | `ca_segment_len > ca_maxlen` | ❌ fatal | 装不下一个段 → 永远触发不了分段（数学必然） |
| 3 | `ca_maxlen < ca_segment_len × _SEGMENT_HEADROOM_RATIO`(1.2) | ⚠️ warning | 无 20% 余量 → 分段消费一抖动就丢帧 → 录像空洞 |

2 与 3 走 `if/elif`：装不下时只报致命那条，不叠加余量告警刷屏。当前配置（段 10s、余量 900/300=3.00×）零告警。校验只发日志、不抛异常（fatal 那条记 ERROR 级日志）。

## 告警 Gate

`append_alarm_record_with_gate(alarm, mode)` 单 `_alarm_lock` 内原子完成闸门去重 + 赋 seq + 入日志。闸门按 `(self.task_id, alarm.metric, mode)` 做固定 5 秒冷却；`task_id` 取自 CQ 不可变身份，无需调用方传入。被拦截的告警不入账。

## 前端消息

`/task/message/{task_id}` 经 `client_manager.get(task_id)` 找活跃 CQ，`get_alarm_snapshot(since_seq)` 原子返回告警增量 + max_seq，并附 `signals_10s`。CQ 只吐自有词汇的原始数据（流名聚合），流名→metric 的展示映射归 router 装配层，不下沉本层。

## client 与 router 的边界（零跨服务依赖 leaf）

client（`ClientManager`/`ClientQueues`）是**零跨服务依赖的中台 leaf**：解耦不靠 DI 注入，而靠「**只吐自有词汇的原始数据 + 展示翻译上移 router**」。CQ 词汇是流名（detector.name）与内部 primitive；任何「流名 → 展示 metric / 中文标签 / stage 别名」的映射都在 router 装配层完成，不下沉本层——本层若引入展示语义，就会与告警/推理服务产生隐性耦合。此边界已落地（对应路由收敛 T4–T5）；对外 wire 侧的 `?client_id=` → `?task_id=` 迁移（T6）**暂缓待前端**，现阶段 `source_ip` 仍是对外路由标识（见下）。

**外部 wire 词汇仍是 `source_ip`，内部运行时键是 `task_id`(int)**：生产前端/集成测试 viewer 按摄像头 IP 连 `/ai/video?client_id=<ip>`、`/terminate?client_id=<ip>`，故 wire 键 `client_id` 值恒为 `source_ip`；边界层经 `find_by_source_ip`（匹配首个，业务不保证唯一）解成当前 run 后转内部 `task_id`。异常/告警/错误响应里保留的 `client_id` 字段名值也 = `source_ip`（诊断标识，非路由键）。T6 迁移完成后 `find_by_source_ip` 垫片可撤。

## 代码来源

- `app/services/client/manager.py`
- `app/services/client/queues.py`
- `app/services/client/config.py`
- `app/services/run_control.py`
- `app/routers/task.py`
- `tests/test_alarm_increment.py`
- `tests/test_task_message_api.py`
- `tests/test_teardown_identity_fence.py`
