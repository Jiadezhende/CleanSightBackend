> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Testing Map

本文件索引当前测试覆盖面，并给后续修改提供优先补测方向。`pytest tests/` 只跑 `tests/`（`pyproject.toml` 的 `testpaths=["tests"]`、`pythonpath=["."]`）；`integration_tests/` 是需要手动起的独立脚本。

## 测试基建：factories + conftest + doubles

> 硬约束：构造 CQ / DetBox / DetectorOutput / FrameDetection / Frame / Alarm / RunIdentity 等领域对象**只走 `tests/factories.py`**，别在用例里另起炉灶或复制构造逻辑；契约一变只改 factories 一处。

- **`tests/factories.py`**（构造单一真源）：纯 builder 函数、**无 pytest 依赖**（可被 `integration_tests/` 复用；pytest 把 `tests/` 插入 sys.path，用例直接 `from factories import ...`）。每个 builder 带「最常见良性态」默认值，用例只写关心的偏差（关键字 override）。
  - `make_det_box`→`DetBox`、`make_detector_output`→`DetectorOutput`（`n=0` 即该帧无检测）、`make_frame_detection`→`FrameDetection`（缺省单流；可带 `cq` 句柄模拟 collector 刚组装的帧）、`make_frame`→`Frame`（全零帧）、`make_alarm`→`Alarm`（默认实时 BUBBLE / LEAK）。
  - `make_cq`→`ClientQueues`：带不可变 `RunIdentity`（一 CQ == 一 run）；给 `run=` 就用它（真落盘时传 `runs.allocate` 的结果），`step_id=None` 得到未绑定 run 的 CQ。`make_bare_cq`：无身份裸建（task / step / stage 取空默认值），给算子 / 纯队列单测用。
  - `make_run(task_id, step_id[, run_id])`：返回该 step 盘上最新的 run，没有就建（给了 `run_id` 按它建，否则 `runs.allocate`）；只建目录，**不保证 `runs.query` 可见**。
  - `seed_hls_segments(task_id, step_id, items, …)`：在 `make_run` 的 run 下铺段文件 + init，**并登记进清单**（`items` 是段名里的 epoch 毫秒 `ts_ms`，可带 EXTINF）。登记这一步不能省：「有哪些段」只由清单回答，光有段文件 = 没有段。
- **`tests/conftest.py`**：
  - factory-as-fixture：`make_cq` / `make_det_box` / `make_frame_detection` 各出一个 fixture，返回 factories 里的**同一纯函数**，与 `from factories import ...` 同源。
  - **autouse `_isolate_storage_root`**：每个用例默认把 `settings.storage_dir` 指到独立临时目录——`start_run` 会 `runs.allocate` 建 run 目录，没显式要 `tmp_storage` 的用例也不能写进真实 `database/`。这是唯一的 autouse，其余 fixture 都要用例显式声明。
  - `tmp_storage`：显式把存储根指到 `tmp_path` 并返回路径（要拿路径造数 / 断言时用）。
  - `fast_task_queue`：`task_queue._POLL_INTERVAL` 0.5s → 0.01s，免得 `SerialTaskQueue.stop()` 每条队列白等半秒。
- **`tests/doubles.py`**（跨文件共用替身的单一落点）：`MockDetector`（纯 numpy 亮度启发式，无权重无 torch）、`BrushRulesSegmenter`（纯规则离线分段；测试 config 里以 `{"class": "doubles.BrushRulesSegmenter"}` 引用）、`FakeProc` / `FakeLauncher` / `offline_result`（离线作业子进程替身，`FakeProc` 由用例手动 `finish()`）、`wait_until`（轮询直到条件成立）。生产代码与配置里没有 MOCK 实现，这些替身只在测试里。测试文件之间不互相 import。
- **刻意不收敛**（集中无收益）：单文件专用的 MagicMock 替身留在本地，如 `test_api_concurrency` 的 `_make_db_task` 与 `patch.object(db_tasks, "query_task")`、`test_inference_stage_routing` 的 fake stage 配置 / factory / cq。
- **提速用的 patch 点**：告警上报退避 patch `alarm_worker` 模块内的 `time`（不真等）；`TestCli` 把 `cli._isolate_cpu` 换成 no-op（否则会在 pytest 主进程里永久改 CUDA 可见性与 torch 线程数）；`mediamtx_gateway/rtsp_proxy.py` 的 `_CONNECT_RETRIES` / `_CONNECT_RETRY_DELAY` 是供测试 patch 的模块常量。
- 静态护栏：`grep "ClientQueues(" tests/` 应仅命中 `factories.py`。

## API 与编排

- `tests/test_api_concurrency.py`：per-task 锁（`ClientService.lock_for`，保留真锁）下的并发编排：同 task 重复 start 幂等、改 url 触发 stop_run 后重建、start + terminate 串行（事件序 enter → exit → stop_stream 断言）、不同 task 互不阻塞（两方 Barrier 断言）、terminate 两种入口都取锁。mock 打在 `app.services.run_control.service.*`。
- `tests/test_start_rollback.py`：`start_run` setup 步失败 → 对称回滚注销 CQ（`stop_run(expected=cq)`），无泄漏；step 非数字 → 400 且不建 CQ；未配置 step 在动旧 run 之前就被拒（同 task 正在跑的 run 不被 stop）。
- `tests/test_task_message_api.py`：实时消息接口的 `since_seq` 校验、task 不在内存返回空、运行中把 `since_seq` 透传给快照。
- `tests/test_task_message_assembler.py`：`app.routers.task` 的前端消息装配（流名 → metric 映射 + 空模板、告警序列化在 router 侧；CQ 只出纯数据）。
- `tests/test_task_alarms_api.py`：`GET /task/{id}/alarms`：DB 行 → DTO 映射、顺序透传；DB 失败 503（让 `SessionLocal` 抛 `SQLAlchemyError`，断言 detail 原文）。
- `tests/test_task_live_history_api.py`：`/task/live`、`/task/history`：清单只出参数不出 URL，断言参数能直接喂给播放端；`/history` 的 `tracks[]` 必须反映盘上实况（只有 raw 的 step 按默认 processed 打 playlist 即 404，这是核心回归点）。DB 替换 `db_tasks.query_source_ips`，段用 `seed_hls_segments` 造。
- `tests/test_admin_serialization.py`：admin 的纯函数部分：`_client_info`、`_quantile`、`_parse_metrics_json`。prometheus 对 Counter 剥 `_total` 后缀（family 名不含 `_total`），`_parse_metrics_json` 按 family 名查 4 个指标（`infer_latency_ms` / `infer_failure` / `frame_drop` / `retry`，定义在 `app/services/utils/metrics.py`），由这里钉住。
- `tests/test_admin_offline_jobs.py`：`/admin-f3m8/offline/jobs`：提交 202 / 未配置 400 / 查询 404 / 列表倒序；服务语义（去重、409 等）在 `test_offline_job_service` 测，路由只透传。
- `tests/test_ai_temporal_router.py`：`POST /ai/temporal`：分段事实读取 + 墙钟 → 媒体刻度换算。
- `tests/test_algorithm_router.py`：`POST /algorithm/colorstrip`：numpy + cv2 合成小图判合格 / 不合格、拒判走 200 + `ok=false`、请求层问题（空串、非法 base64、非图片字节、超上限、档名写错）走 400、data URL 前缀可吃。不依赖真实样本；算法判据本身的回归在仓库外的验收工装（见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)）。
- `tests/test_static_mount.py`：`/ui-f3m8` 一个挂载出 admin / lab 两页与 5 个共用 vendor 文件，各页没有私有 vendor 目录。

## 客户端状态与生命周期 fence

- `tests/test_cq_state_machine.py`：CQ 状态机 + 写门 + `close()`：迟到写入在写入时刻被拒。
- `tests/test_teardown_identity_fence.py`：`stop_run` 先封闸（DRAINING）再停生产者 / 落盘，拆完 CLOSED；HealthMonitor「先决策后拿锁」窗口内槽位被 `/start` 换新时，`stop_run(expected=旧cq)` 整段放弃。
- `tests/test_writeback_handle_fence.py`：写回只写捕获的 `frame.cq`、不按 client_id 反查：旧 CQ 非 ACTIVE 时三写（slide_window / latest_detection / `ca_detections`）全被挡并计 stale_run；同批 ACTIVE run 不受殃及；推理降级帧进帧窗 / 快照但不进落盘缓冲。
- `tests/test_client_detections_buffer.py`：`ca_detections` 落盘缓冲：写门、满时丢最旧并计数、`drain` 原子全排空、`close` 释放；与滑窗共锁但语义相反，各测各的。
- `tests/test_client_service_find_by_source_ip.py`：`ClientService.find_by_source_ip` 命中多个时取 `task_started_at` 最晚者。
- `tests/test_alarm_increment.py`：告警 gate（固定冷却窗口）+ seq 自增：`append_alarm_record_with_gate` 通过即记录、被拦即不记。
- `tests/test_cq_pressure_log.py`：三条 CA 队列的压力日志在 `append_*` 内驱动，且不改变入队 / 丢帧 / 返回值行为。

## 推理（在线）

- `tests/test_inference_stage_routing.py`：`resolve_stage` 路由、不可跑的 step（未定义 / 无 detector）被拒、detector 构造失败即启动失败、无 detector 的 stage 不致命、真 `InferenceService` 初始化不变式 + `stop_workflow` 冒烟。
- `tests/test_stage_worker_ts_anchor.py`：多 detector 聚合后同帧各流 `DetectorOutput.timestamp` 相等且等于帧捕获 ts（`StageWorker._infer_models` 穿透），供 `FrameDetection` 对齐与帧窗算子推进游标。
- `tests/test_operator_framework.py`：Operator 框架契约：subscribes 注入、工厂 fail-fast、感受野 `_clip`、帧窗投影、缓冲按感受野、per-operator 隔离。
- `tests/test_temporal_debounce.py`：Operator 边沿触发去抖：`analyze` 推进共享状态机、`judge` 出 rising edge 告警、`finalize` 结算（弯曲不足）。
- `tests/test_inference_resample.py`：`resample_by_ts` 按 ts 相位网格降采样（在线 Operator / 离线 Segmenter 共用）：2:1 间隔、1:1 全保留、缺口重锚、strict 拒慢输入。
- `tests/test_alarm_sink.py`：`alarm_sink.persist_alarms` 直接读 `alarm.stage`（不反解 alias）、过闸被拒时跳过落库；落库出口 monkeypatch `alarm_service.persist_alarm`。
- `tests/test_infer_proxy.py`：`RemoteInferProxy` 主进程侧——req_id 关联、pending 有界、子进程失败后的孤儿清理。**不 spawn 真子进程、不碰 GPU**（绕过 `_spawn_child`，注入假 `req_q` 手动置 `child_ready`）。
- `tests/test_dispatcher_round_robin.py`：`StageAwareDispatcher._drain_and_submit` 的轮转均衡、被拒不丢帧（帧原封留 deque）、稳态每 stage 每轮一批。
- `tests/test_pipeline_drop_counters.py`：dispatcher 的静默丢帧计数（`_stage_queues` 满 → `get_stage_drops()`；proxy 拒收 → `_stage_rejects`，并入压力行）。
- `tests/test_pressure_reporter.py`：`PressureReporter` 到点且有压力才打一行、平稳静默、delta 相对上次打印（注入假钟）。
- `tests/test_viz_throughput_snapshot.py`：`[VIZ_THROUGHPUT]` 仅压力时打印，并归因 supply-bound / render-bound / viz-starved。
- `tests/test_rounded_rect_roi.py`：`_draw_rounded_rect` ROI 局部化与整帧实现像素级等价。

## 告警上报与异常边界

- `tests/test_exception_handling.py`：告警上报重试（`app/services/alarm/alarm_worker.py`）：retry 指标、fatal / retryable / 次数上限判定、退避序列 `[1.0, 2.0]`（patch 模块内 `time`，不真等）。
- `tests/test_boundary_layers.py`：只测边界层 3——FastAPI 全局异常 handler 的 HTTP 映射（stream / database / ffmpeg / inference / persistence / generic 6 条；fixture 临时挂路由、用完摘除）。docstring 列出的边界层 1（`worker_guard.guarded_run`）在本文件里没有用例。

## 离线推理与作业

- `tests/test_offline_pipeline.py`：分段替换（`_replace_segments`）、配置工厂、`require_offline` 分支、`OfflineRunner`（read-merge-write、运行锁、run 被回收 → reclaimed 且不写、旁路形状不一致不落 npz 但事实照写）、clean 策略（打桩前向，含降采样与因果窗口）、CLI 退出码。不依赖 GPU / RTSP / DB / 网络。
- `tests/test_offline_job_service.py`：`OfflineJobService` 串行、按 run 去重、运行中 run 409、取消 / 停机 / 超时 kill、结果解析；子进程全用 `doubles.FakeLauncher`。
- 离线产物的读侧端点：`test_admin_offline_jobs.py`、`test_ai_temporal_router.py`、`test_lab_label_probs.py`（见各自小节）。
- 真 CLI 子进程：`integration_tests/test_offline_job_subprocess.py`（见「集成测试」）。

## 数据层 `app/storage/`

全部落在临时存储根（autouse 隔离；要拿路径时显式用 `tmp_storage`），不碰真实 `database/`。

- `tests/test_storage_tasks.py`：`storage.utils.root` 的根解析（相对路径以项目根为基、与 cwd 无关，`TestRootPath`）+ `tasks` 的定位 / 枚举。只断言路径与目录事实，不涉及产物格式。
- `tests/test_storage_fs.py`：`storage.utils.fs` 盘上原语：整体替换、原子删除（rename 进 `.trash/`）、清回收区、建一级目录。
- `tests/test_storage_runs.py`：`RunIdentity` 值语义、`runs.allocate`（run_id = epoch 毫秒，时钟停滞 / 回拨也严格递增）、`runs.query` 可见判据、`query_latest_by_step`、域读写口只收 `RunIdentity` 的落位与建目录边界。
- `tests/test_storage_hls.py`：`{step}/{run_id}/hls/` 的定位、编解码与写入事务，hls 域唯一的单测文件。覆盖段名 / sidecar / EXTINF 往返（`ts_ms` 向下取整只在毫秒域闭合）、`insert_segment` 的 stage/commit 事务（假编码器）、选段与 `query_span` / `query_has_*`、媒体轴（`TestTimelineLoad / Select / WallFromMedia / MediaFromWall / WallGaps`）、tfdt patch、`effective_fps`、段级 / 帧级裁剪与解码契约；真 cv2 + ffmpeg 的端到端用例缺外部工具时 skip。
- `tests/test_storage_inference.py`：`{step}/{run_id}/inference/` 下 `detections.jsonl`（追加）/ `temporal.jsonl`（原子整体替换）/ `label_probs.npz` 的 codec 往返（detections 投影有损、temporal 无损）、失败整体作废旧文件保留、落位隔离、坏行隔离、`query_has_offline_results`。
- `tests/test_storage_cleanup_ttl.py`：TTL 判据 = `{task}/{step}/` 目录自身 mtime（= 最近一次 `runs.allocate`；写段不续命、开新 run 续命），整个 step（含所有 run）一起删；删除走 `fs.remove`（先 rename 进 `.trash/`），回收区每轮先清；非数字目录不碰；rename 失败 step 原样保留。

## 平台 DB `app/db/`

- `tests/test_db_queries.py`：`app.db.tasks` / `app.db.alarms` 的只读查询。`SessionLocal` 换成内存 SQLite 上的 sessionmaker，条件 / 排序 / 分页按真 SQL 执行；失败路径用未建表的引擎触发真 `OperationalError`。SQLite 与 PostgreSQL 的 NULL 排序相反，用例不造 NULL。
- 其余 router 用例的 DB 替身一律 monkeypatch `db_tasks.query_*` / `db_alarms.query_*`，只在验 503 原文时让 `SessionLocal` 抛异常。

## 录制与落盘编排

- `tests/test_recording_service.py`：**只钉编排**，格式怎么落盘归 `test_storage_hls.py`：按 run 落盘（同 step 重启后旧一代迟到段写进它自己的 run）、未绑定 run / 空批 / 队列未起或满即拒收、`collect_from` 整段先于残段、断流 flush 挂起请求按身份核对与拆除回收、`SegmentSweeper`、detections 落盘（`TestSubmitDetections` / `TestDetectionRunIsolation` / `TestCollectAndFlushDetections`）。末尾端到端跑真队列 + 真 `storage.hls` / `storage.inference`，缺外部工具时 skip。
- `tests/test_task_queue.py`：`app.services.utils.task_queue.SerialTaskQueue` 的提交序 == 执行序 + 串行性 + 停机语义。HLS 落盘不加目录锁，「同轨相邻段 tfdt 不错位」全押在这上面——这里松一寸，那边就是静默的回放跳段。
- `tests/test_cq_drain_fence.py`：`drain_ca_*` 的时间戳栅栏，即断流 flush 的切点。全排空会把重连后的帧跟断流前的帧拼进同一段，`effective_fps` 由首末帧跨度反推、跨度里混着整个 gap ⇒ 10 秒画面写成 30 秒慢放，且 fps 仍落在 `[1,60]` 内不触发退化兜底，**全程无一条报警**。文件末尾另测 `take_raw_segment` / `take_processed_segment`（恰好弹出 seg_len 帧、不足一段返回 None 且残帧保留、积压逐段拉完）。

## 帧 sidecar 往返

**两处互补，动 sidecar 写读顺序或裁剪边界时都要跑**——边界数学与「解出来的像素是不是那一帧」是两回事：

- `tests/test_storage_hls.py` 的 `TestSegmentLevelTrim` / `TestFrameLevelTrim` / `TestDecodeTrackContract` / `TestDecodeCommand`（进 `pytest tests/`）：seam 单测，不起 ffmpeg，抓段级 / 帧级边界。
- `integration_tests/test_hls_frame_roundtrip.py`（手动跑）：**唯一能抓 ts↔像素错配的手段**。只依赖 ffmpeg；直接走 `hls.insert_segment` 真写路径，再用 `hls.iter_frames` / `hls.read_segment` 按 ts 读回，帧内中心色块编码 frame_id（三通道 16 阶量化抗 H.264 有损）逐帧比对。默认 task 9900002，结束即删（`--keep` 保留）。

## 分层与导入门禁

`tests/test_import_hygiene.py`：**必须起子进程**——pytest 主进程早被别的用例把 torch/cv2 装进 `sys.modules`，在本进程里测等于没测。9 条门禁：

```text
test_import_budget（参数化 BUDGET）              干净子进程 import 后不得出现 HEAVY（torch / ultralytics / cv2）、耗时 < 上限；
                                                 FORBIDDEN_APP_IMPORTS 另查 offline.cli 不拉起 app.main / app.routers /
                                                 inference.online / services.stream
test_layer_package_modules_are_all_budgeted      4 个 LAYER_PACKAGES 里每个模块都有 BUDGET 条目
test_layer_package_imports_only_whitelisted_app_modules
                                                 分层包的 app.* 白名单：app/storage、app/db、app/services/algorithm（仅自身）、
                                                 app/services/utils
test_singleton_reference_surface                 8 个 SINGLETONS 只许被 run_control/service.py、routers/*、本包 __init__ 与
                                                 SINGLETON_EXCEPTIONS（health_monitor/worker.py、alarm_sink.py）import
test_services_do_not_import_routers              services ↛ routers
test_daemons_do_not_import_routers               daemons ↛ routers
test_services_do_not_import_daemons              services ↛ daemons
test_alarm_does_not_import_inference             alarm ↛ inference
test_intra_package_relative_cross_package_absolute
                                                 包内相对、跨包绝对、相对不上翻（含 mediamtx_gateway/）
```

方向门禁与白名单都按 AST 查，相对导入先还原成绝对再判。新增任何分层包模块必须同时在 `BUDGET` 加一行，否则 `test_layer_package_modules_are_all_budgeted` 直接红。

## 流与重连

- `tests/test_stream_rewrite.py`：RTSP URL 内部端口改写（`_rewrite_rtsp_url`）。
- `tests/test_reconnect_on_initial_failure.py`：重连判据 = decoder 子进程死活：`start()` 失败后 decoder 仍登记供监控接管；进程死 → 重连、进程活 → 不重连、未注册 → orphan 路径；完整 respawn 状态机；无帧超 `cleanup_timeout` → cleanup（纯时间触发）；槽位被 `/start` 换新的身份 fence。
- `tests/test_rtsp_read_timeout.py`：`-timeout` 必须来自 `settings.rtsp_read_timeout_s`（不硬编码），外加与 `cleanup_timeout` 的串联预算护栏。钉的是实测结论——静默断流下 ffmpeg 要连续两次读超时才退出，**判死延迟 = 2 × `-timeout`**。
- `integration_tests/test_single_client.py --scenario 2/3`：断流重连（成功 / 超时自动清理）端到端场景。

## Gateway

- `tests/test_gateway.py`：`GatewayMiddleware` 三层防护——IP 白名单、速率限制、反扫描，HTTP 与 WebSocket 两条路径，以及清理线程。
- `tests/test_mediamtx_gateway.py`：`mediamtx_gateway`：配置加载、RTSP TCP 代理（转发 / 封禁 / 限流 / 目标不可达）、MediaMTX 进程守护（正常退出 / stop_event / 崩溃重启 / 超限停止）。Windows / Linux 双平台可跑。

## 追溯与媒体

- `tests/test_traceback_router.py`：`/traceback/task/{id}/playlist.m3u8` 与 `/timeline`（必填 step_id）、`/media/segment/{token}` 合法下载 / 伪造拒绝、路径穿越防御（判据是 `hls.parse_*_name` 解不解得出身份键）。
- `tests/test_media_token.py`：`app.routers.utils.media_token` 的签发 / 校验、错误 secret / 签名 / kind / 过期 / 格式、路径穿越 filename 拒签、未配 secret 时的 ephemeral 默认单例。
- `tests/test_router_utils_runs.py`：`app.routers.utils.runs.resolve_timeline` 取 run 与该轨媒体轴，拿不到就 404；404 文案与 `resource_id` 与 ai `/temporal`、lab `/label-probs` 逐字一致。
- `tests/test_media_timeline.py`：只测断流判定（`app/services/utils/media_timeline.py` 的 `GAP_THRESHOLD_MS` / `first_gap` / `total_gap_ms`）；媒体轴本身的用例在 `test_storage_hls.py`。
- `tests/test_utils_vod_playlist.py`：`app.services.utils.vod_playlist` 的 VOD m3u8 文本渲染（纯函数，全文逐字节 + `map_uri` 必填 + `TARGETDURATION` 用 ceil）。
- `/task/history` 两阶段扫描见「API 与编排」的 `test_task_live_history_api.py`。

## Lab

- `tests/test_lab_service.py`：送标服务本体 `app.services.lab.service`：LS 配置校验、project 解析、clip 校验、`submit_clips`、`export_step`、`ping_label_studio`；fake ClipBuilder / fake LabelStudioClient monkeypatch 到 service 模块，不跑 ffmpeg、不连 LS。
- `tests/test_lab_router_submit.py`：`/lab-f3m8/submit`、`/download`、`/health` 的 HTTP 层：检查顺序、错误体、service 结果到 DTO 的映射。
- `tests/test_lab_clip_builder.py`：`ClipBuilder.build_one` 装配（媒体刻度进、绝对墙钟出；区间非法 / 出界 / 跨空洞三档拒绝）+ `_run_ffmpeg` 命令形态（HLS demuxer、清单与段同目录、`-ss` 在输出侧、缺 init fail-fast、失败也清理）；另有 AST 检查 router 构造参数与 `services/lab/service.py` 同步。
- `tests/test_lab_step_exporter.py`：整段导出：EXTINF 取 playlist 真值、在途段不算数、临时 m3u8 落 `{run}/hls/`、HLS demuxer + `-c copy`、缺 init fail-fast、失败 finally 清理、孤儿回收、track 选择。
- `tests/test_lab_tasks_api.py`：`GET /lab-f3m8/tasks` 的 storage 模式清单。
- `tests/test_lab_label_probs.py`：`POST /lab-f3m8/label-probs` 与 lab 任务清单的 `offline_steps`。

## 集成测试 `integration_tests/`

均为独立脚本（`python integration_tests/<file>.py …`），不进 `pytest tests/`。

| 文件 | 依赖 | 覆盖 |
|---|---|---|
| `test_hls_frame_roundtrip.py` | 只要 ffmpeg | 帧 sidecar ts↔像素往返（见上） |
| `test_offline_job_subprocess.py` | 不需后端 / RTSP / DB / GPU | `OfflineJobService` 真起 `python -m app.services.inference.offline.cli run`：起进程 / env / cwd / 末行 JSON 解析；存储根经 `CLEANSIGHT_STORAGE_DIR` 指到临时目录 |
| `test_traceback.py` | 后端可达 + DB | `/traceback`、`/media` 端到端；预置 DB 与盘上数据（默认 task / alarm 9900001），结束自动清理 |
| `test_single_client.py` | 后端 + 推流 | scenario 1–9：正常流程、断流重连成功 / 失败、仅推流、仅 start、延迟推流、CLEAN 阶段、未配置 step → `/api/start` 400、阶段切换；观测走 `/ui-f3m8/admin/` |
| `test_multi_client.py` | 后端 + 推流 + DB | 从 DB 取多个任务并发跑正常流程 |

`cleanup_processes.py` 清理上述脚本残留的推流进程；`fixtures/` 只入库 `test_video.mp4`。

## 覆盖率

`pytest-cov` opt-in：配置在根 `pyproject.toml` 的 `[tool.coverage.*]`（`source = ["app", "mediamtx_gateway"]`、`branch = true`、`omit = ["app/main.py", "*/__pycache__/*"]`）。**不入 addopts、不设 `--cov-fail-under` 门禁**；按需：

```bash
pytest tests/ --cov --cov-report=term-missing   # 用不带值的 --cov；--cov=app 会覆盖 source、漏掉网关
```

**缺口分两类，策略相反**：

- **桶 1 — I/O 边界，集成-only，低覆盖是有意**：`stream/decoder.py`、`storage/hls/_encode.py` 与 `_fmp4.py`（编码 / 转码腿）、`inference/online/detection/{stage_worker,infer_proxy}.py`（CUDA 推理与子进程）、`inference/online/visualization/visualizer.py`、`routers/ai.py`（WS 推流循环）、`services/alarm/alarm_worker.py` 与 `services/recording/sweep_worker.py`（线程循环）。硬写单测＝测 mock 不测真实行为，负 ROI；纯逻辑（`effective_fps` / 切段 / ROI）已抽出单测，真实保障靠 `integration_tests/` + 远程真流审计。
- **桶 2 — 纯函数轻缺口，补单测**：如 `routers/admin.py` 的序列化（`test_admin_serialization.py`）。

## 建议补测

- `app/services/utils/worker_guard.py::guarded_run`（worker 线程自愈，边界层 1）没有单测。
- 新增检测任务时，补 Detector / Operator 单元测试和 YAML 加载测试。
- 修改 HLS 写入逻辑时，补 playlist EXTINF、timeline end_ms 测试。
- 新增分层包模块（`app/storage`、`app/db`、`app/services/algorithm`、`app/services/utils`）时，**必须同时在 `test_import_hygiene.py` 的 `BUDGET` 加一行**，否则门禁直接红。
- 修改清理流程时，补结算告警归属和残余段 flush 测试。
- 修改 Gateway 配置时，补 relaxed / bypass / normal 三类路径测试。
- 修改 Lab 上传时，补单段失败不影响整请求的响应结构测试。
- 动 sidecar 写读顺序或裁剪边界时，`test_storage_hls` 的 Trim / Decode 类 + `integration_tests/test_hls_frame_roundtrip.py` 两个都要跑。

## 代码来源

- `tests/`（`factories.py` 构造单一真源、`conftest.py` 共享 fixture、`doubles.py` 测试替身）
- `pyproject.toml`（pytest `testpaths` / `pythonpath` + 覆盖率配置）
- `integration_tests/`
- `app/storage/`、`app/db/`
- `app/services/recording/`、`app/services/inference/`、`app/services/alarm/`、`app/services/algorithm/`
- `app/daemons/`
- `app/routers/`（含 `routers/utils/`）
