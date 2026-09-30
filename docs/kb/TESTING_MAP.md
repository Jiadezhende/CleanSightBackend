> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Testing Map

索引当前测试覆盖面与补测方向。`pytest tests/` 只跑 `tests/`（`pyproject.toml`：`testpaths=["tests"]`、`pythonpath=["."]`）；`integration_tests/` 是手动运行的独立脚本。下文 `tests/` 下的文件省略目录前缀。

## 测试基建：领域对象只经 factories 构造

> 硬约束：CQ / DetBox / DetectorOutput / FrameDetection / Frame / Alarm / RunIdentity 等领域对象**只走 `tests/factories.py`** 构造，契约一变只改一处。护栏：`grep "ClientQueues(" tests/` 应只命中 `factories.py`。

- **`factories.py`**：纯 builder 函数，无 pytest 依赖（`integration_tests/` 可复用）；默认值是最常见良性态，用例只写偏差。
  - `make_det_box` / `make_detector_output` / `make_frame_detection` / `make_frame` / `make_alarm`。
  - `make_cq`：带不可变 `RunIdentity`（`step_id=None` 得到未绑定 run 的 CQ）；`make_bare_cq`：无身份裸建，给算子 / 纯队列单测。
  - `make_run(task_id, step_id[, run_id])`：取该 step 盘上最新 run，没有就建；只建目录，**不保证 `runs.query` 可见**。
  - `seed_hls_segments(...)`：铺段文件 + init **并登记进清单**——「有哪些段」只由清单回答，不登记等于没有段。
- **`conftest.py`**：唯一的 autouse `_isolate_storage_root` 把每个用例的 `settings.storage_dir` 指到独立临时目录（`start_run` 会建 run 目录）；`tmp_storage` 显式指到 `tmp_path` 并返回路径；`fast_task_queue` 把 `task_queue._POLL_INTERVAL` 调到 0.01 s；`make_cq` 等 fixture 与 factories 同源。
- **`doubles.py`**（跨文件共用替身的唯一落点）：`MockDetector`（numpy 启发式，无 torch）、`BrushRulesSegmenter`（测试 config 以 `doubles.BrushRulesSegmenter` 引用）、`FakeProc` / `FakeLauncher` / `offline_result`（离线作业子进程替身）、`wait_until`。生产代码与配置里没有 MOCK 实现；测试文件之间不互相 import；单文件专用的 MagicMock 替身留在本地。
- 提速 patch 点：告警退避 patch `alarm_worker` 模块内的 `time`；`TestCli` 把 `cli._isolate_cpu` 换成 no-op（否则永久改 pytest 主进程的 CUDA 可见性与 torch 线程数）；`mediamtx_gateway/rtsp_proxy.py` 的 `_CONNECT_RETRIES` / `_CONNECT_RETRY_DELAY`。

## API 与编排

- `test_api_concurrency.py`：per-task 真锁下的编排：重复 start 幂等、改 url 触发重建、start + terminate 串行、不同 task 互不阻塞、terminate 两种入口都取锁；`/api/start` DB 边界（任务不存在 404 / `source_ip` 为空 400 / DB 失败 503，均不进 `start_run`）。
- `test_start_rollback.py`：setup 失败对称回滚无泄漏；step 非数字 400 且不建 CQ；未配置 step 在动旧 run 之前被拒。
- `test_run_control_shutdown.py`：`RunControlService.shutdown()` 对每个在跑 run 停 decoder、收并上报 settlement、`flush_residual`、注销 CQ，不调 `stream.shutdown`。
- `test_task_message_api.py` / `test_task_message_assembler.py`：`since_seq` 校验与透传、task 不在内存返回空；前端消息装配（流名 → metric 映射 + 空模板，告警序列化在 router 侧）。
- `test_task_alarms_api.py`：DB 行 → DTO 映射与顺序；DB 失败 503 并断言 detail 原文。
- `test_task_live_history_api.py`：只出参数不出 URL；`/history` 的 `tracks[]` 反映盘上实况（只有 raw 的 step 按默认 processed 请求 playlist 会 404，核心回归点）、运行中任务排除、最多 10 条、DB 失败降级。
- `test_admin_serialization.py`：`_client_info` / `_quantile` / `_parse_metrics_json`；钉住 prometheus Counter 的 family 名不含 `_total`。
- `test_admin_offline_jobs.py`：`/admin-f3m8/offline/jobs` 路由层：202、未配置 400、未知 job / 无可见 run / 未知 run_id 404、运行中 409、列表倒序、按 run_id 查询。
- `test_ai_temporal_router.py`：`POST /ai/temporal` 分段换算到媒体轴并吸附空洞、`run_id` 点名、无结果空、无段 404、非法 body 422。
- `test_algorithm_router.py`：合成小图判合格 / 不合格、拒判走 200 + `ok=false`、请求层问题走 400。算法判据回归在仓库外的验收工装（见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)）。
- `test_static_mount.py`：`/ui-f3m8` 一个挂载出 admin / lab 两页与 5 个共用 vendor 文件，各页无私有 vendor 目录。

## 客户端状态与生命周期 fence

- `test_cq_state_machine.py`：CQ 状态机 + 写门 + `close()`；DRAINING 放行 settlement 告警、CLOSED 拒绝。
- `test_teardown_identity_fence.py`：`stop_run` 先封闸再 flush、flush 时 CQ 仍在注册表、拆完 CLOSED；`expected` 不符时整段放弃。
- `test_writeback_handle_fence.py`：写回只写捕获的 `frame.cq`：非 ACTIVE 旧 CQ 的三写（slide_window / latest_detection / `ca_detections`）全被挡并计 stale_run；推理降级帧不进落盘缓冲。
- `test_client_detections_buffer.py`：`ca_detections` 写门、满时丢最旧并计数、`drain` 原子全排空。
- `test_client_service_find_by_source_ip.py`：多个命中取 `task_started_at` 最晚者。
- `test_alarm_increment.py`：告警 gate（冷却窗口，被拦不续期）+ seq 自增。
- `test_cq_pressure_log.py`：CA 队列压力日志不改变入队 / 丢帧 / 返回值行为。

## 推理（在线）

- `test_inference_stage_routing.py`：`resolve_stage` 路由与拒绝、detector 构造失败即启动失败、无 detector 的 stage 不致命、真 `InferenceService` 初始化 + `stop_workflow` 冒烟。
- `test_stage_worker_ts_anchor.py`：多 detector 聚合后同帧各流 `DetectorOutput.timestamp` 都等于帧捕获 ts。
- `test_operator_framework.py`：Operator 契约：subscribes 注入、工厂 fail-fast、感受野裁剪、帧窗投影、per-operator 隔离。
- `test_temporal_debounce.py`：气泡边沿触发、弯折去抖与 `finalize` 结算。
- `test_inference_resample.py`：`resample_by_ts`（在线 Operator / 离线 Segmenter 共用）：2:1、1:1、缺口重锚、strict 拒慢输入。
- `test_alarm_sink.py`：`persist_alarms` 直读 `alarm.stage`、过闸被拒不落库。
- `test_infer_proxy.py`：`RemoteInferProxy` 主进程侧：req_id 关联、pending 有界、子进程失败后孤儿清理、ready 判定；不 spawn 子进程、不碰 GPU。
- `test_dispatcher_round_robin.py` / `test_pipeline_drop_counters.py`：轮转均衡、被拒不丢帧；stage 队列满与 proxy 拒收的计数并入压力行。
- `test_pressure_reporter.py` / `test_viz_throughput_snapshot.py`：有压力才打、限频、delta 相对上次打印；`[VIZ_THROUGHPUT]` 归因 supply-bound / render-bound / viz-starved。
- `test_rounded_rect_roi.py`：`_draw_rounded_rect` ROI 局部化与整帧实现像素级等价。

## 告警上报与异常边界

- `test_exception_handling.py`：`alarm_worker.py` 重试：fatal / retryable / 次数上限、retry 指标、退避序列 `[1.0, 2.0]`。
- `test_boundary_layers.py`：只测边界层 3（FastAPI 全局异常 handler 的 6 条 HTTP 映射）；边界层 1（`worker_guard.guarded_run`）无用例。

## 离线推理与作业

- `test_offline_pipeline.py`：`_replace_segments`、离线 Segmenter 工厂、`require_offline`、`OfflineRunner`（read-merge-write、运行锁、回收的 run 不写、npz 形状不对只跳旁路）、clean 策略（打桩前向）、CLI 退出码。
- `test_offline_job_service.py`：串行、按 run 去重、运行中 run 409、取消 / 停机 / 超时 kill、结果解析（`doubles.FakeLauncher`）。
- 真 CLI 子进程见 `integration_tests/test_offline_job_subprocess.py`。

## 数据层 `app/storage/` 与平台 DB `app/db/`

- `test_storage_tasks.py`：根解析（相对路径以项目根为基、与 cwd 无关）+ `tasks` 定位与枚举。
- `test_storage_fs.py`：整体替换、原子删除（rename 进 `.trash/`）、清回收区、建一级目录。
- `test_storage_runs.py`：`runs.allocate` 严格递增（时钟停滞 / 回拨）、`runs.query` 可见判据、`successor`、`query_latest_by_step`、域读写口只收 `RunIdentity`。
- `test_storage_hls.py`：hls 域唯一单测文件：段名 / init 名 / sidecar / 清单编解码、`insert_segment` 事务、选段与 `query_*`、媒体轴（`TestTimeline*` / `TestWallFromMedia` / `TestMediaFromWall`）、tfdt patch、`effective_fps`、裁剪与解码契约；端到端用例缺 cv2 / ffmpeg 时 skip。
- `test_storage_inference.py`：`detections.jsonl`（追加，投影有损）/ `temporal.jsonl`（原子替换，无损）/ `label_probs.npz` 往返、坏行隔离、`query_has_offline_results`。
- `test_storage_cleanup_ttl.py`：TTL 判据 = `{task}/{step}/` 目录 mtime（开新 run 续命、写段不续命），整个 step 一起删；先 rename 进 `.trash/`；非数字目录不碰。
- `test_db_queries.py`：`app.db` 只读查询跑在内存 SQLite 上（真 SQL），失败路径用未建表引擎触发真 `OperationalError`；SQLite 与 PostgreSQL 的 NULL 排序相反，用例不造 NULL。其余 router 用例的 DB 替身一律 monkeypatch `db_tasks.query_*` / `db_alarms.query_*`。

## 录制与落盘编排

- `test_recording_service.py`：只钉编排（落盘格式归 `test_storage_hls.py`）：按 run 落盘（旧一代迟到段写进它自己的 run）、拒收条件、`flush_residual`、`collect_from` 整段先于残段、断流 flush 请求的身份核对与拆除回收、detections 落盘。
- `test_task_queue.py`：`SerialTaskQueue` 提交序 == 执行序、串行、停机语义。HLS 落盘不加目录锁，「同轨相邻段 tfdt 不错位」全押在这上面，失守即静默的回放跳段。
- `test_cq_drain_fence.py`：`drain_ca_*` 的时间戳栅栏（断流 flush 切点）。全排空会把重连前后的帧拼进同一段，`effective_fps` 被 gap 拉低却仍在 `[1,60]` 内，10 秒画面写成 30 秒慢放且无任何报警。另测 `take_*_segment`。

## 帧 sidecar 往返：seam 单测与集成往返都要跑

动 sidecar 写读顺序或裁剪边界时：

- `test_storage_hls.py` 的 `TestSegmentLevelTrim` / `TestFrameLevelTrim` / `TestDecodeTrackContract` / `TestDecodeCommand`：不起 ffmpeg，抓段级 / 帧级边界。
- `integration_tests/test_hls_frame_roundtrip.py`：唯一能抓 ts↔像素错配的手段。只依赖 ffmpeg，走 `hls.insert_segment` 真写路径后按 ts 读回，用帧内色块编码的 frame_id 逐帧比对。默认 task 9900002，结束即删（`--keep` 保留）。

## 分层与导入门禁：test_import_hygiene 必须起子进程

pytest 主进程早已装入 torch / cv2，在本进程里测等于没测。9 条门禁：

```text
test_import_budget                       干净子进程 import 后不得出现 torch / ultralytics / cv2、耗时 < 上限；
                                         另查 offline.cli 不拉起 app.main / app.routers / inference.online / services.stream
test_layer_package_modules_are_all_budgeted
                                         4 个分层包的每个模块都有 BUDGET 条目
test_layer_package_imports_only_whitelisted_app_modules
                                         app/storage            → app.storage / app.types / app.settings
                                         app/db                 → app.db / app.types / app.settings
                                         app/services/algorithm → 仅自身
                                         app/services/utils     → 自身 / app.storage / app.types / app.settings
test_singleton_reference_surface         8 个服务单例只许被 run_control/service.py、routers/*、本包 __init__ 与
                                         health_monitor/worker.py、alarm_sink.py import
test_services_do_not_import_routers      services ↛ routers
test_daemons_do_not_import_routers       daemons ↛ routers
test_services_do_not_import_daemons      services ↛ daemons
test_alarm_does_not_import_inference     alarm ↛ inference
test_intra_package_relative_cross_package_absolute
                                         包内相对、跨包绝对、相对不上翻（含 mediamtx_gateway/）
```

按 AST 查，相对导入先还原成绝对再判。新增分层包模块必须同时在 `BUDGET` 加一行，否则直接红。

## 流与重连

- `test_stream_rewrite.py`：RTSP URL 内部端口改写。
- `test_reconnect_on_initial_failure.py`：重连判据 = decoder 子进程死活：`start()` 失败后 decoder 仍登记；死 → 重连（登记一次残帧 flush）、活 → 不重连、未注册 → orphan；respawn 状态机；无帧超 `cleanup_timeout` → cleanup；槽位换新时的身份 fence。
- `test_rtsp_read_timeout.py`：`-timeout` 取自 `settings.rtsp_read_timeout_s`，外加与 `cleanup_timeout` 的预算护栏。钉的实测结论：静默断流下 **判死延迟 = 2 × `-timeout`**。
- 断流重连端到端：`integration_tests/test_single_client.py --scenario 2/3`。

## Gateway

- `test_gateway.py`：IP 白名单、速率限制、反扫描三类 store 与清理线程；HTTP 覆盖 relaxed（用生产默认 `gateway_relaxed_prefixes` 参数化，含 `/admin-f3m8/overview`）/ bypass（跳过限流与反扫描）/ normal（超限封禁升级）三档；WebSocket 封禁与限流时 close。
- `test_mediamtx_gateway.py`：配置加载、RTSP TCP 代理（转发 / 封禁 / 限流 / 目标不可达）、MediaMTX 进程守护（正常退出 / stop_event / 崩溃重启 / 超限停止）。

## 追溯与媒体

- `test_traceback_router.py`：playlist（VOD 生成、缺 init 503、结构化 404、step 隔离、token 锁定 run）、timeline（告警打点、DB 挂降级、非法 `detected_at`、媒体坐标与空洞、`gap_total_ms`、告警限于 run 存续期、无 run 全 0）、未知 `run_id` 404、`/media/*` token 校验与非段名拒绝。
- `test_media_token.py`：签发 / 校验的各类拒绝、带 `/` 的 filename 拒签、未配 secret 时的 ephemeral 单例。
- `test_router_utils_runs.py`：`resolve_timeline` 的 404 文案与 `resource_id` 与 `/ai/temporal`、`/lab-f3m8/label-probs` 逐字一致。
- `test_media_timeline.py`：只测断流判定（`GAP_THRESHOLD_MS` / `first_gap` / `total_gap_ms`）。
- `test_utils_vod_playlist.py`：`render_vod` 全文逐字节、`map_uri` 必填、`TARGETDURATION` 取 ceil。

## Lab

- `test_lab_service.py`：`services/lab/service.py` 全部函数；fake ClipBuilder / LabelStudioClient，不跑 ffmpeg、不连 LS。
- `test_lab_router_submit.py`：`/submit` 检查顺序与 DTO 映射、`/download` 无 run 404 且不导出、`/health`。
- `test_lab_clip_builder.py`：`build_one`（墙钟换算、非法 / 超长 / 出界 / 跨空洞拒绝、越过轨尾收短时长）、`_run_ffmpeg` 命令形态（HLS demuxer、清单与段同目录、`-ss` 在输出侧、缺 init fail-fast、失败清理）；AST 检查 `service.py` 构造 `ClipBuilder` / `ClipSpec` 时传的 kwarg 都在签名里。
- `test_lab_step_exporter.py`：EXTINF 取清单真值、在途段不算、临时 m3u8 位置、`-c copy`、缺 init fail-fast、失败清理、孤儿回收、track 选择。
- `test_lab_tasks_api.py`：`GET /lab-f3m8/tasks` 的 db 与 storage 模式（排序、分页、过滤）。
- `test_lab_label_probs.py`：概率换算到媒体轴、无产物空、无段 404、缺身份 422；storage 清单项带 `offline_steps`。

## 集成测试 `integration_tests/`

独立脚本（`python integration_tests/<file>.py …`）。

| 文件 | 依赖 | 覆盖 |
|---|---|---|
| `test_hls_frame_roundtrip.py` | ffmpeg | 帧 sidecar ts↔像素往返 |
| `test_offline_job_subprocess.py` | 无（不需后端 / RTSP / DB / GPU） | `OfflineJobService` 真起离线 CLI 子进程：env / cwd / 末行 JSON 解析 |
| `test_traceback.py` | 后端 + DB | `/traceback`、`/media` 端到端；预置 task / alarm 9900001，结束自动清理 |
| `test_single_client.py` | 后端 + 推流 | scenario 1–9：正常、断流重连成功 / 失败、仅推流、仅 start、延迟推流、CLEAN、未配置 step 400、阶段切换 |
| `test_multi_client.py` | 后端 + 推流 + DB | 多任务并发正常流程 |

`cleanup_processes.py` 清理残留推流进程；`fixtures/` 只入库 `test_video.mp4`。

## 覆盖率：opt-in，不设门禁

`pyproject.toml` 的 `[tool.coverage.*]`：`source = ["app", "mediamtx_gateway"]`、`branch = true`、omit `app/main.py`。不入 addopts、不设 `--cov-fail-under`：

```bash
pytest tests/ --cov --cov-report=term-missing   # 用不带值的 --cov；--cov=app 会覆盖 source、漏掉网关
```

I/O 边界模块低覆盖是有意的，靠集成保障：`stream/decoder.py`、`storage/hls/_encode.py` 与 `_fmp4.py`、`inference/online/detection/{stage_worker,infer_proxy}.py`、`inference/online/visualization/visualizer.py`、`routers/ai.py`（WS 推流循环）、`alarm/alarm_worker.py` 与 `recording/sweep_worker.py`（线程循环）。其中的纯逻辑（`effective_fps` / 切段 / ROI）已抽出单测；其余纯函数缺口应补单测。

## 建议补测

- `app/services/utils/worker_guard.py::guarded_run`（边界层 1，worker 线程自愈）没有单测。
- 进程停机的残段落盘未经端到端验证：`test_run_control_shutdown.py` 只打桩验调用；需在真实 RTSP 下 Ctrl-C，核对 run 目录最后一段与 `detections.jsonl` 尾部。
- 新增检测点时补 Detector / Operator 单测与 YAML 加载测试。

## 代码来源

- `tests/`（`factories.py`、`conftest.py`、`doubles.py`）、`integration_tests/`
- `pyproject.toml`
