> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Health Monitor Service

全局健康监控是自动化治理组件，负责断流重连、任务超时、孤儿状态清理和统一 cleanup。全程按 int `task_id` 键。

## 启动位置与协作者

生命周期归本包自己的 `lifespan()`（`app/services/health_monitor/__init__.py`），在 `app/main.py` 的嵌套 lifespan 里是**最外层**：最先起、最后停，全程看着 stream / persistence / recording / inference 四层。单例在 `instance.py`，`routers/health.py` 只读它做状态查询。

`GlobalHealthMonitor.__init__` 的五个入参（`client_manager` / `stream_service` / `inference_manager` / `config` / `recording_service`）**一律可缺省且构造期零副作用**：不读 yaml、不碰全局单例，缺省者一并推迟到 `start()` 里的 `_resolve_deps()` 现取（四个 import 都写在函数体内——顶层拉 inference 会把 torch/YOLO 链拽进来，recording 则拽 numpy/cv2）。已注入的值 `start()` 不覆盖，测试传 mock 即可。

## 检测对象

每轮检查读取：

- `client_manager.snapshot()`（`{task_id → ClientQueues}`）。
- StreamService 中所有 decoder 的 task_id。
- 每个 ClientQueues 的 `latest_raw_timestamp`。

内部状态按 task_id 键：`_reconnecting_clients: {task_id → ReconnectState}`、`_last_activity: {task_id → float}`。

## 断流与重连（判据 = decoder 进程死活）

判据是**后端 decoder 子进程是否存活**（`stream_service.is_decoder_alive(task_id)`），**不是**帧 staleness。依据：实测 RTSP 断流时后端 ffmpeg 从 TCP 控制通道即收 EOF 退出（decoder 的 `-timeout` 兜底把「真·网络分区」下的挂死也转成退出），故进程死活能干净区分——

- **进程已退出**（断流 EOF / 崩溃 / 首启失败）→ 进入重连模式，按 `reconnect_interval` 节流反复 `restart_stream()`（respawn）；某次 respawn 起活进程并来足够新的新帧 → 退出重连（成功）。比旧的 5s staleness 判据更快感知。
- **进程活着但暂无帧**（等首个关键帧 / 瞬时停）→ **只等，不杀**（根治「等首帧被误杀→重连→再等一个 GOP」的启动延迟翻倍 bug）。

`ReconnectState`（`types.py`）字段：`task_id`、`stream_url`、`last_attempt_time`（respawn 节流）、`last_frame_time_before_disconnect`（判新帧）、`cq`（身份 fence）；`attempt_count` 已停用（不再数次数，保留字段作兼容）。无 fps/protocol 字段（固定 RTSP、fps 走配置）。

重连成功的判据是**真来了新帧**（`latest_raw_timestamp` 超过断流前那一帧、且 `frame_age < heartbeat_timeout`），不是「进程活着」——respawn 后的新进程在等首个关键帧时也活着但无帧，此刻判成功或重杀都是错的。进程活着却还没来帧一律只等。

## 判死延迟吃掉重连预算（串联护栏）

静默断流（真·网络分区，对端不发 FIN）下 decoder 不会立刻退出，要等 ffmpeg 的读超时。**判死延迟 = `2 × settings.rtsp_read_timeout_s`**（2× 是 ffmpeg demux 层的两次等待，配置改不掉，见 [SERVICE_STREAM.md](SERVICE_STREAM.md)）。

这段时间全记在 `cleanup_timeout` 的账上（后者从最后一帧算起），于是两处配置是**串联**的：

```text
最后一帧 ──── 2×rtsp_read_timeout_s ────> decoder 退出 ──── 剩余预算 ────> cleanup 拆除
             （判死，期间监控只看到"进程还活着且无帧"）      respawn + 建连 + 等首个关键帧
             └──────────────────── cleanup_timeout（默认 20s）──────────────────┘
```

当前默认：`rtsp_read_timeout_s=2.5` ⇒ 判死 ~5.4s，留给重连 ~15s。`GlobalHealthMonitor.start()` 末尾调 `_check_reconnect_budget()` 做越界检查，**只告警不纠正**（两个值各有正当运维理由，代码没资格替人选）：

- `2T ≥ cleanup_timeout` → ERROR「配置冲突」：进程还没死就先被判 cleanup 拆除，重连永不触发。
- 剩余预算 < `cleanup_timeout/2` → WARNING：判死占掉过半预算。

其余阈值与它的关系：`check_interval`（1s）是感知粒度——进程死后下一 tick 即进重连；`reconnect_interval`（5s）是两次 respawn 之间的节流；`heartbeat_timeout`（5s）现在只作重连成功的新帧新鲜度阈值，不再有「可疑」区间。

## 断流时登记残帧 flush（与 recording 的唯一协作点）

`_enter_reconnect_mode` 在写入 `_reconnecting_clients` **之后**调 `recording_service.request_residual_flush(cq, fence_ts=last_frame_time)`——让断流造成的空洞落到**段边界**上，而不是被某一段吞进去。

- **时机必须是「进入重连时」**，不能等重连成功：成功的判据就是「已经来了新帧」，那时残批里已混进重连后的帧，段照样横跨 gap（只是被吞的比例变了）。
- **必须放在 `_reconnecting_clients` 赋值之后**：`_check_all_clients` 对已在重连表里的 task 直接 `continue`，放在后面 = 每次断流恰好登记一次；放到函数开头会被上面 `stream_info` 缺失的早退带成每 tick 重复登记。
- **只登记、不就地 drain**：重连期 CQ 仍在注册表里、recording 的 sweeper 还在扫它，就地 drain 会变成第二个 drain 者。两次 drain 各自被 CQ 锁保护、帧不重不漏，但 `submit_segment` 发生在锁外，谁先入队由调度决定——**入队序一乱 tfdt 就乱**（每段 tfdt = 入队执行时读到的累计 EXTINF），清单 ts 不再升序、读侧段级定位失效。走队列挡不住这个：队列只保证执行序 = 入队序。
- **重连成功时用同一个栅栏再登记一次**（`_handle_reconnecting_client` 判成功后、`_exit_reconnect_mode(cleanup=False)` 之前）：raw 轨由 decoder 直写、进重连那刻队列内容就定了，而 processed 轨由 viz worker 按 tick 从推理结果渲染、ts 落后 raw 一个推理管线延迟；延迟超过「检测 + sweeper 一个 tick」时，断流前的 processed 帧在首次 flush 之后才入队，没人再切它们。重复登记安全：栅栏是时间戳，重连后的帧 ts 都大于它，第二次只捞迟到的断流前帧，没有就 drain 出空列表、什么都不提交。

**不登记的后果是静默的**：断流那刻攒在 CA 队列里的半批帧被重连后的帧补满，拼成横跨 gap 的段；`eff_fps` 由首末帧跨度反推、跨度里混进整段 gap → 10 秒画面写成 30 秒 EXTINF，3× 慢放，回放/导出/送标三条链路一起中招。而 `eff_fps≈9.99` 仍落在合理带 `[1,60]` 内不触发退化兜底，**全程无一条报警，每次重连必现**。

`recording_service` 是本类的第 5 个协作者，与其余四个同款：构造注入 + `_resolve_deps()` 里函数体内 import 取全局单例。本模块只用它这一个方法；flush 的实际执行、段打包与队列语义全归 recording（见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)）。

## 清理条件

触发清理的情况：

- 重连中**无帧时长 ≥ cleanup_timeout**（配置项，默认 20s；纯时间触发，重连本身不数次数——`max_reconnect_attempts` 连同「heartbeat + interval×attempts」的派生式已一并删除）。
- 任务运行超过 `task_max_duration`（类默认 7200s，**现网 yaml 配 1800s**；0=禁用）。
- 有 ClientQueues 但无 decoder，超过 `orphan_timeout`（默认 30s）。
- 有 decoder 但无 ClientQueues，立即停止孤儿 decoder。

## cleanup_client → 委托 RunController

`cleanup_client(task_id, reason, skip_decoder=False, expected=None)` 是统一清理入口（API terminate 走 api→RunController，重连失败/孤儿/超时走本入口），**直接委托** `run_controller.stop_run(task_id, reason, skip_decoder=skip_decoder, expected=expected)`——不再各自调 `InferenceManager.remove_client` / `ClientManager.remove_client`。

`expected` 传监控线程在**决策时刻**捕获的 CQ 对象引用：monitor 线程「先决策后拿锁」，决策→拿锁间槽位可能被 `/start` 重启换新 CQ，`stop_run` 内以 `expected` 做对象身份 fence，槽位已非 expected 则整段放弃，防误删健康新 run。拆机顺序（封闸→停 decoder→落 settlement/HLS→清 registry）由 RunController 统一，见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## 代码来源

- `app/services/health_monitor/manager.py`（`GlobalHealthMonitor`）
- `app/services/health_monitor/__init__.py`（`lifespan()`）、`instance.py`（单例）
- `app/services/health_monitor/config.py`
- `app/services/health_monitor/types.py`
- `app/services/run_control.py`（`cleanup_client` 委托的 `stop_run`）
- `app/services/recording/service.py`（`request_residual_flush`）
- `app/settings.py`（`rtsp_read_timeout_s`，预算护栏的另一半）
- `app/routers/health.py`（只读状态查询）
- `config/health_monitor_config.yaml`
- `tests/test_rtsp_read_timeout.py`（预算护栏三档边界）
- `tests/test_reconnect_on_initial_failure.py`
- `tests/test_teardown_identity_fence.py`

