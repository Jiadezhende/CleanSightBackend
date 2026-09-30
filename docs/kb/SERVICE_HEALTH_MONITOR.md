> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Health Monitor（daemon）

按时钟自驱的后台 daemon：断流重连、任务超时、孤儿状态清理，拆除一律委托 `RunControlService.stop_run`。全程按 int `task_id` 键。

## 位置与依赖：构造零副作用，协作者在 start() 里取

包 `app/daemons/health_monitor/`：`worker.py`（`HealthMonitorWorker`）、`instance.py`（单例 `health_monitor_worker`）、`config.py`、`types.py`（`ReconnectState`）；`__init__.py` 只有 docstring + `lifespan()`。它在 `app/main.py` 的 lifespan 里是最外层：最先起、最后停。`routers/health.py` 只读单例查状态（daemons 不 import routers，门禁 `test_daemons_do_not_import_routers`）。

`__init__` 的五个入参（`client_service` / `stream_service` / `inference_service` / `config` / `recording_service`）都可缺省，构造期不读 yaml、不碰单例；缺省者在 `start()` 的 `_resolve_deps()` 里用函数体内 import 现取（顶层拉 inference 会带进 torch/YOLO），已注入的 mock 不覆盖。导入预算门禁锁住这点：import 本包及 `.instance` 不拉起任何 `app.services`（`tests/test_import_hygiene.py` BUDGET）；取别家单例是引用面门禁的具名例外（`SINGLETON_EXCEPTIONS`）。`run_control_service` 不在 `_resolve_deps()` 里，在 `cleanup_client()` 函数体内另取。

## 每 tick 做什么

监控线程每 `check_interval` 调一次 `_check_all_clients()`，读 `client_service.snapshot()` 与 `stream_service.get_all_task_ids()`。自有状态只有 `_reconnecting_clients: {task_id → ReconnectState}` 与统计计数。

对注册表里每个 `(task_id, cq)`，按顺序：

1. 已在重连表 → `_handle_reconnecting_client`，本轮不再看别的。
2. `task_max_duration > 0` 且 `now - cq.task_started_at ≥ task_max_duration` → 超时拆除。
3. 有 decoder：
   - `is_decoder_alive` 为 False → `_enter_reconnect_mode`；
   - 进程活着但距最后一帧 ≥ `cleanup_timeout` → 拆除（最后防线，真挂死正常已被 decoder 的 `-timeout` 转成进程退出）；
   - 进程活着、无帧未超时 → 只等。
4. 无 decoder → 距 `latest_raw_timestamp` ≥ `orphan_timeout` 即按孤儿流拆除（`skip_decoder=True`）。

之后对「有 decoder 无 CQ、且不在重连表」的孤儿 decoder，持 `lock_for(task_id)` 直接 `stop_stream`（没有 CQ 可拆，不经 `stop_run`）。

## 断流判据是 decoder 进程死活，不是帧 staleness

实测 RTSP 断流时 ffmpeg 收 EOF 即退出，网络分区下的挂死也被 `-timeout` 转成退出，所以进程死活能干净区分：进程已退出（断流 / 崩溃 / 首启失败）→ 重连；进程活着但暂无帧（等首个关键帧 / 瞬时停）→ 只等、不杀，杀掉会让启动延迟翻倍（等首帧 → 重连 → 再等一个 GOP）。

- **重连**：进程仍死时每 `reconnect_interval` 调一次 `restart_stream()`，不设次数上限；进程活着就只等。
- **成功的判据是真来了新帧**：`latest_raw_timestamp` 大于断流前最后一帧且 `frame_age < heartbeat_timeout`。respawn 后的新进程等关键帧时也活着但无帧，此刻判成功或重杀都错。
- **放弃的判据是纯时间**：重连中无帧 ≥ `cleanup_timeout`，不数重连次数。
- **`ReconnectState`**：`task_id`、`stream_url`、`last_attempt_time`（节流）、`last_frame_time_before_disconnect`（判新帧、兼作残帧栅栏）、`cq`（身份 fence 基准，须在进入重连时捕获）。每 tick 若槽位已不是 `state.cq`（被 `/start` 换了新 run），放弃重连、删条目，不动新 run。

## 判死延迟吃掉重连预算：两处配置是串联的

静默断流（对端不发 FIN）下 decoder 要等 ffmpeg 读超时才退出，判死延迟 = `2 × settings.rtsp_read_timeout_s`（2× 的由来见 [SERVICE_STREAM.md](SERVICE_STREAM.md)）。`cleanup_timeout` 从最后一帧算起，这段时间全记在它账上：

```text
最后一帧 ──── 2×rtsp_read_timeout_s ────> decoder 退出 ──── 剩余预算 ────> cleanup 拆除
             （监控只看到"进程活着且无帧"）               respawn + 建连 + 等首个关键帧
             └──────────────────── cleanup_timeout（默认 20s）──────────────────┘
```

默认 `rtsp_read_timeout_s=2.5` ⇒ 判死约 5.4 s，留给重连约 15 s。`start()` 末尾的 `_check_reconnect_budget()` 只告警、不纠正：

- `2T ≥ cleanup_timeout` → ERROR「配置冲突」：进程还没退就被拆除，重连永不触发。
- 剩余预算 < `cleanup_timeout / 2` → WARNING：判死占掉过半预算。

## 断流时登记残帧 flush：本模块与 recording 的唯一协作点

HM 只负责在两个时刻调 `recording_service.request_residual_flush(cq, fence_ts=断流前最后一帧 ts)`，flush 的执行、时机约束与不登记的后果（3× 慢放）见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md) 断流残帧一节。HM 侧的约束：

- **首次登记放在 `_enter_reconnect_mode` 写入 `_reconnecting_clients` 之后**：已在重连表里的 task 每 tick 直接走重连分支，放这里恰好每次断流登记一次；放到函数开头，会因 `stream_info` 缺失的早退而每 tick 重复登记。
- **重连判定成功后、`_exit_reconnect_mode(cleanup=False)` 之前用同一栅栏再登记一次**，捞走迟到的断流前 processed 帧。
- **只登记、不就地 drain**：重连期 CQ 仍在注册表里、sweeper 还在扫它，就地 drain 会变成第二个 drain 者。

## 拆除一律委托 RunControlService

`cleanup_client(task_id, reason, *, skip_decoder=False, expected=None)` 是 HM 的统一拆除入口（`/api/terminate` 不经这里，直接调 `stop_run`）：先 pop 本类的 `_reconnecting_clients` 条目，再调 `run_control_service.stop_run(task_id, reason, skip_decoder=…, expected=…)`。

`expected` 传监控线程在**决策时刻**捕获的 CQ：重连失败传 `state.cq`，任务超时与孤儿流传本轮 snapshot 里的 `cq`。HM「先决策后拿锁」，其间槽位可能被 `/start` 换新，`stop_run` 据此做对象身份 fence。拆除顺序见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## 配置

`config/health_monitor_config.yaml` 的 `monitor:` 段 → `HealthMonitorConfig`：

| 项 | 类默认 | 现网 yaml | 作用 |
|----|--------|-----------|------|
| `check_interval` | 1.0 s | 1.0 | 感知粒度：进程死后下一 tick 即进重连 |
| `heartbeat_timeout` | 5.0 s | 5.0 | 只作重连成功的新帧新鲜度阈值 |
| `reconnect_interval` | 5.0 s | 5.0 | 两次 respawn 之间的节流 |
| `cleanup_timeout` | 20.0 s | 20.0 | 无帧放弃时限（重连中与「活着无帧」两处共用） |
| `orphan_timeout` | 30.0 s | 30.0 | 有 CQ 无 decoder 的清理时限 |
| `task_max_duration` | 7200 s | 1800 | 任务最长运行时长，0 = 不限 |

## 代码来源

- `app/daemons/health_monitor/{__init__,instance,worker,config,types}.py`、`config/health_monitor_config.yaml`
- `app/services/run_control/service.py`（`stop_run`）、`app/services/recording/service.py`（`request_residual_flush`）
- `app/services/stream/service.py`（`is_decoder_alive` / `restart_stream`）
- `app/settings.py`（`rtsp_read_timeout_s`）、`app/routers/health.py`
- `tests/test_rtsp_read_timeout.py`（预算护栏三档边界）、`tests/test_reconnect_on_initial_failure.py`、`tests/test_teardown_identity_fence.py`
