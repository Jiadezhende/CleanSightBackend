> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 存储与 Schema

后端同时使用平台数据库（只读）和本地文件目录。本文只写现状；数据层准入判据与条文编号（R/W/L/D/T、路线 A/B/C）见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)。

## 数据模型分三层：types / ORM / DTO

- `app/types/`：跨层共享契约，纯 dataclass / enum，零服务依赖（除 numpy 外无框架依赖），是依赖图的叶子。分文件：`frame.py`(`Frame`) / `detection.py`(`DetBox`, `DetectorOutput`, `FrameDetection`) / `temporal.py`(`TemporalEvent`, `TemporalSegment`, `LabelProbs`) / `alarm.py`(`AlarmType`, `AlarmMetric`, `Alarm`) / `run.py`(`RunIdentity`) / `exceptions.py`(`AppError` 体系)。标记型 `__init__`，从子模块 import。
- `app/db/`：平台 DB 的 ORM，一张表一个模块，各带只读 `query_*`。
- DTO：HTTP 请求 / 响应模型就地定义在各 router。
- 不进 types 的：渲染契约 `RenderSpec` / `RenderItem` / `RenderType`（`inference/online/render.py`）；online 内部入参 `DetectionTask`（`online/types.py`）。

关键归位：

- **`Alarm` 是单一告警抽象**：核心字段由 Operator 填；`stage` 由 temporal actor 烧入；`mode` 由 `online/temporal/alarm_sink.persist_alarms` 在落库边界补；`seq` 由 CQ 闸门 `append_alarm_record_with_gate` 赋。`AlarmMetric` 由 Judge / Operator 显式设定，不由下游反推。
- **`RunIdentity(task_id, step_id, run_id)`**：frozen、按值比较、只含身份不含路径；只由 `app.storage.runs` 的 `allocate` / `query` 产出。CQ 持 `cq.run`。
- **时序契约**（`app/types/temporal.py` docstring 三条硬约束）：① `ts` / `start` / `end` 是帧捕获墙钟 ts，epoch **浮点秒**，与 `FrameDetection.ts`、HLS `.idx` 同源同值（「对外时间一律 int 毫秒」的具名例外）；② `producer` 是产出者身份唯一真源；③ `meta` 只放伴随观测量，不放被代码读来做判断的键。身份键由落盘路径携带，`type` 判别字段归 storage codec。`LabelProbs` 是可视化旁路，不参与任何判断。

### `FrameDetection` 是唯一的帧级检测对象，在线写回、落盘、离线回放同型

`FrameDetection` = `ts` + `by_source: Dict[流名, DetectorOutput]` + `frame_width` / `frame_height: Optional[int]` + 写回路由句柄 `cq`。三粒度：`DetBox`（一个框）→ `DetectorOutput`（一个检测器 × 一帧）→ `FrameDetection`（所有检测器 × 一帧）。

- **分辨率沿每帧轴透传，不是检测器输出**：唯一采集点是 `RemoteInferProxy.submit` 从原始帧 `shape` 盖章进 `_Pending`（`online/detection/infer_proxy.py`），随 collector 组装、写回、落盘、回读走。缺省 None，消费方走默认兜底。online `temporal/impl/clean.py::_adapt_to_features` 与 offline `impl/clean.py::_collect_object_arrays` 都从这里读。
- `DetectorOutput.metadata` 只装检测器级数据（`model`、失败时的 `error`）；`DetBox.extra` 当前零写者、不落盘。
- **`cq` 只在 collector → 写回口这一段有值**：`online/detection/service.py::_write_back_results` 取走后置 None，留存的帧一律不带。

## 平台数据库（`app/db/`）只读

读取只经 `app.db` 的 `query_*`（`from app.db import tasks as db_tasks` / `alarms as db_alarms`）；告警写入走外部 HTTP 上报（[SERVICE_ALARM.md](SERVICE_ALARM.md)）。

- **连接**（`database.py`）：`QueuePool`，`pool_pre_ping=True`，常驻 5、溢出 10、回收 3600 s；连接串 `settings.database_url`（`CLEANSIGHT_DB_*` 拼接）。
- **session**：每个 `query_*` 自开自关，返回会话已关闭的 ORM 实例，只能读列属性。
- **失败语义**：`SQLAlchemyError` 一律包成 `DatabaseError(retryable=True)`（边界层转 503）。降级归调用方：traceback timeline 退化成空 events、`/task/history` 的 `source_ip` 置 null、其余 503。构造错误时刻意不传 `task_id=`，否则 `str(exc)` 多出 `[task=…]`，改掉 503 detail。
- **调用方**只有 routers（api / task / traceback / lab）；`database.get_db` 零调用方。`app/storage` 与 `app/db` 互不依赖（`LAYER_PACKAGES`）。

### clean_task（`app/db/tasks.py::DBTask`）

关键字段：`_id`（平台主键 varchar）；`task_id`（业务主键，有索引，**运行键即取此**）；`source_ip`（被动：诊断 + 遗留 wire 适配，不是路由键）；`current_step`（字符串，`RunControlService` 边界 `int()` 转 step_id）；`status`、`updated_time`、`start_time`、`end_time`。

| 查询 | 语义 |
|---|---|
| `query_task(task_id)` | 按业务主键取一行，无则 None（`/api/start`） |
| `query_source_ips(task_ids)` | 单次 IN 查询 → `{task_id: source_ip}`；空输入直接 `{}`、不开 session |
| `query_task_page(needle, *, limit, offset)` | `(total, rows)`；needle 对 source_ip / status 做 ilike，能转 int 时再 OR task_id；`updated_time desc, task_id desc`（lab db 模式） |

### clean_alarm（`app/db/alarms.py::DBAlarm`）

关键字段：`alarm_id`、`task_id`、`step_id`、`step_name`、`alarm_type`、`severity`、`message`、`detected_at`、`resolved`、`create_time`。

| 查询 | 语义 |
|---|---|
| `query_task_alarms(task_id)` | 该 task 全部告警，`create_time` 降序 |
| `query_step_alarms(task_id, step_id)` | 该 step 告警，`detected_at` 升序 |
| `detected_at_ms(v)` | 按量级归一到毫秒（`<1e11` 秒、`<1e14` 毫秒、其余微秒）；None / ≤0 → `ValidationError`。读 `detected_at` **只能走它** |

## 文件落盘：一次运行（run）一个目录，run 下按域隔离

落盘根 `settings.storage_base_dir`（由 `storage_dir` 以项目根为基推导）；数据层内唯一解析点 `app/storage/utils/root.py::_storage_root()`。

```text
{storage_base_dir}/
  {task_id}/{step_id}/{run_id}/          run_id = 分配时刻 epoch 毫秒，同 step 内严格递增
    hls/        {track}_segment_{ts_ms}.mp4   fMP4 fragment，mdhd.timescale 固定 90000
                {track}_init.mp4              按轨各一份（两轨独立 playlist，不可互指）
                {track}_playlist.m3u8         LIVE 形态，只追加、不写 ENDLIST
                raw_segment_{ts_ms}.idx       raw 轨逐帧 ts sidecar（float64 原值），仅供离线反查
                .stage_{track}_{ts_ms}/       insert_segment 暂存，事务结束即删
                .clip_*.m3u8 / .export_*.m3u8 lab 临时 VOD 清单，用完即删（不匹配段名正则）
    inference/  detections.jsonl / temporal.jsonl / label_probs.npz
                .{name}.tmp                   路线 C 暂存，换名后即消失
  .trash/                                 回收区（utils.fs.remove 的 rename 目标）
  .lab_exports/                           lab 临时件（位置可由 settings.lab_export_temp_dir 改）
  lab_runtime_config.json                 lab 运行时配置（app/services/lab/runtime_config.py）
```

- **盘上不变式**：step 的直接子项只有 run 目录（纯数字名）；run 下只有 `DOMAINS = ("hls", "inference")` 域目录、没有文件。存储根下除数字 task 目录与 `.trash/` 外，只有层外自己拼路径的 `.lab_exports/` 与 `lab_runtime_config.json`。
- **run 目录只由 `runs.allocate` 建**；域写口只建域这一级（`utils.root.domain_dir(create=True)`），run 被回收后的迟到写 `FileNotFoundError`，不重建僵尸目录。
- **盘上原语**（`utils/fs.py`）：`replace`（同目录 tmp → `os.replace`，不建父目录）、`remove`（rename 进 `{root}/.trash/{uuid}` 再 rmtree，三态 `ABSENT` / `REMOVED` / `FAILED`，失败只 warning 不抛）、`purge_trash`、`ensure_dir`（不带 parents）。包外唯一调用方 `app/daemons/cleanup/worker.py`。
- **`.lab_exports/` 只有一半有回收**：整段导出件 `step_*.mp4` 由 `StepExporter._sweep_orphans` 按 30 min 扫孤儿；送标 clip 作业目录用完即删，但 `keep_artifacts_on_failure` 时保留，**无任何自动回收**（cleanup daemon 只认数字目录名）。
- **时间量纲**：`run_id` 与段名 / `.idx` 名 / stage 名 / 段 URI 里的 `ts_ms` 是 int 毫秒；inference 三份产物与 `.idx` **内容**里的 ts 是浮点秒。

### run：分配、查询、可见性（`app/storage/runs.py`）

| 成员 | 语义 |
|---|---|
| `allocate(task, step)` | `run_id = max(time_ns // 1_000_000, 已有最大 + 1)`，再 `mkdir(parents=True)`；不带 `exist_ok`——目录已在说明分配没串行，抛 `OSError`。调用方须持 `lock_for` |
| `query(task, step, run_id=None)` | 给了 `run_id`：目录在就返回（不判可见），不在 → None。缺省：按 run_id 降序返回第一个**可见** run |
| 可见判据 `_visible` | `inference/detections.jsonl` 存在，或任一轨清单里有段（`hls.query_has_segments`）。只影响缺省查询 |
| `successor(run)` | 同 step 下紧接着分配的 run_id，不看可见性；最新 → None |
| `query_latest_by_step(task)` | 各 step 的最新可见 run，按 step 升序；无可见 run 的 step 不出现 |
| `query_lifespan_ms(run)` | `[run.run_id, successor)` 墙钟毫秒；最新 run 上界 None |

- **唯一分配调用点** `RunControlService.start_run`：在 `client_service.lock_for(task_id)` 内、早于 CQ 构造与 `client_service.set`；`OSError` 包成 `AppError`（500）。起流失败回滚不回收已分配的 run 目录，空目录随 step TTL 清。
- **换代不删旧产物**：同 step 重启 = 新 run 写新目录，旧 run 留到 step TTL，可按 `run_id` 点名回放 / 离线分析。
- **新 run 可见早于首段录像**，不带 `run_id` 的缺省查询分三段（接受的行为）：分配后到 `detections.jsonl` 出现前（约 1 s），新 run 不可见，回落到上一个可见 run；此后到首段 hls 登记前（10 s+），新 run 已可见但无段，回放返回 404 / 空、不再回落；首段登记后正常。
- **读侧只解析一次 run**：入口统一经 `app/routers/utils/runs.py`（`resolve_run` / `resolve_timeline` / `resolve_media_run` / `no_run`），此后整次请求只读这个 run；MediaToken 可选键 `"r"` 把 run 锁进段 URL（接线见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)）。

### `hls/`：播放产物

- **写者唯一** `RecordingService`（`hls.insert_segment`）；lab 另往 `hls/` 放用完即删的临时 VOD 清单。**读者**：`routers/{media,task,traceback,lab}.py`、`routers/utils/runs.py`（ai / lab 经它）、`services/lab/{clip_builder,step_exporter}.py`、`storage/runs.py`，全部经 `app.storage.hls`。`services/utils/media_timeline.py` 只用 `MediaTimeline` 类型做断流判定，不碰盘。
- **commit 顺序** `sidecar → init → 段文件 → 清单条目`（W8）。只有 raw 轨产 `.idx`；格式与「ts 位级精确」契约见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)。
- **「有哪些段」只由 `{track}_playlist.m3u8` 回答**：盘上有文件但清单没条目 = 不是段（在途或登记失败）。段数 / 时长走 `query_span`，不存统计文件。
- 段身份键 `SegmentRef(track, ts_ms)`，`ts_to_ms(ts) = floor(Fraction(ts) * 1000)`，保证「段名 ≤ 首帧」（不能写成 `int(ts * 1000)`，见 DESIGN_STORAGE_LAYER §8 T2）。`HlsSpan.end_ms = max(ts_ms + round(EXTINF × 1000))`。

对外面（`hls/__init__.py` 的 `__all__`；定位 / 读写口首参 `run: RunIdentity`）：

```text
写      insert_segment
读帧    read_segment / iter_frames                （只服务 raw 轨；生产无调用方，为 ROI 视觉特征预留）
枚举    list_segments / list_segments_in_range
定位    segment_path / init_path / sidecar_path / playlist_path
命名    segment_name / init_name / parse_segment_name / parse_init_name / ts_to_ms
媒体轴  MediaTimeline / PlacedSegment / query_timeline
查询    HlsSpan / query_span / query_has_segments / query_has_init
形状    Segment / SegmentRef / TRACKS
```

### `inference/`：推理产物

`_detection` 管检测层（L1）产物，`_temporal` 管时序层（L3）产物与逐帧概率。对外 7 个成员只收 `RunIdentity`：`append_detections` / `read_detections` / `read_temporal` / `write_temporal` / `read_label_probs` / `write_label_probs` / `query_has_offline_results`。刻意没有：`append_temporal`（`TemporalEvent` 零生产者）、`iter_detections`、`*_path`、删除成员。

| 产物 | 货币 | 写路线 | 写者 | 读者 |
|---|---|---|---|---|
| `detections.jsonl` | `FrameDetection` | B（空批 no-op 且不建目录） | `RecordingService._write_detections`（detections 队列线程） | `OfflineRunner`；`runs` 可见判据 |
| `temporal.jsonl` | `TemporalEvent \| TemporalSegment` | C；空序列照写空文件 | `OfflineRunner._replace_segments`（唯一写者） | `POST /ai/temporal`（只取 Segment）、`offline.cli query`、`query_has_offline_results` |
| `label_probs.npz` | `LabelProbs` | C | `OfflineRunner._maybe_write_label_probs` | `POST /lab-f3m8/label-probs`、`query_has_offline_results` |

`detections.jsonl` 每行一帧（`_detection.py` 的 `_frame_to_record` / `_record_to_frame`）：

```json
{"ts": <float>, "detections": {"<流名>": [{"bbox": [x1,y1,x2,y2], "conf": <float>, "cls_id": <int>, "cls": "<class_name>"}, ...], ...}, "frame_width": <int>, "frame_height": <int>}
```

- `ts` 与在线滑窗、raw `.idx` 同源同值，可按 ts 精确对上同帧录像。
- 每框只落 `bbox / conf / cls_id / cls`，不落 `DetBox.extra`、`metadata`、`success / error`。空框列表的 source 照落（「没检出」≠「没有该流」）。分辨率仅当两者皆非 None 时落。
- **降级帧不落盘**（任一源 `success=False`，`online/detection/service.py`）：格式不带 `success`，空框会被离线当成「没检出」。失败时段在离线侧是时间空洞，整 run 全失败则离线 `skipped`。
- **写失败不重试**：`OSError` 由 `SerialTaskQueue` 记 error 吞掉；纯追加，重试会写重复帧。
- **回读**按 ts 升序（契约）；缺文件返回 `[]`；坏行 / 形状不对的 record 跳过 + warning；`utf-8-sig` 容忍 BOM。格式演进不做迁移。

`temporal.jsonl` / `label_probs.npz`：

- `temporal.jsonl` 每条一行，`type: "event" | "segment"` 判别；`read_temporal` 不排序（两型无共同时间键）。
- **`write_temporal` 是整体替换**，调用方须 read → 合并 → write。`_replace_segments` 丢**全部**旧 `TemporalSegment`、保留 `TemporalEvent`（一个 stage 至多一个离线模型）。同 run 跨进程并发跑离线无互斥；「该 run 正在运行」由作业服务在提交时 409 挡。
- `label_probs.npz`：`ts` float64（无损）、`probs` **float16**（有损，读回 float32）、`labels` unicode；`allow_pickle=False`；不存在返回 None，损坏直接抛。Runner 先写它、后写 `temporal.jsonl`：事实落盘即表示本次运行完成，旁路必须先到。
- `query_has_offline_results`：`temporal.jsonl` 里有 `TemporalSegment`，或 `label_probs.npz` 存在（只判存在）；只有 `TemporalEvent` 不算。消费方：lab 任务列表的 `offline_steps`。

### 跨域 `tasks.py` 与读侧 `query_*`

| 成员 | 语义 / 返回 | 消费方 |
|---|---|---|
| `tasks.list_task_ids(order="id"\|"recent")` | 数字 task 目录；`"recent"` 按 `latest_run_id` 降序，同值 task_id 大者优先，无 run 的 task 排最后但仍在 | `routers/task.py`、`routers/lab.py` |
| `tasks.latest_run_id(task)` | 所有 run 目录的最大 run_id（不看产物），只配剪枝 | `routers/task.py` |
| `tasks.list_step_ids(task)` | step id 升序，**不过滤空 step** | `runs.query_latest_by_step` |
| `runs.query` | `Optional[RunIdentity]` | `routers/utils/runs.py`、`offline/{runner,cli}.py` |
| `runs.query_latest_by_step` | `List[RunIdentity]` | `routers/task.py`（history）、`routers/lab.py`（storage 模式列表） |
| `runs.query_lifespan_ms` | `(start_ms, Optional[end_ms])` | `routers/traceback.py`（timeline 告警按 run 存续期过滤） |
| `hls.query_span` | `Optional[HlsSpan]` | `routers/{task,traceback,lab}.py` |
| `hls.query_has_segments` / `query_has_init` | `bool` | `runs` 可见判据、`routers/lab.py`；`routers/traceback.py`（playlist） |
| `hls.query_timeline` | `MediaTimeline` | `routers/traceback.py`、`routers/utils/runs.py`、`services/lab/clip_builder.py` |
| `inference.query_has_offline_results` | `bool` | `routers/lab.py` |

### TTL 回收：按 step 目录 mtime 整 step 删

实现 `app/daemons/cleanup/`（`worker.py::CleanupWorker`、`instance.py`、`config.py`）。配置 `config/persistence_config.yaml` 的 `storage:` 段：`enable_cleanup`（代码缺省 False / yaml true）、`cleanup_days`（7 / 15）、`cleanup_interval_seconds`（3600）。扫描根委托 `settings.storage_base_dir`。`enable_cleanup` 为假时 `lifespan()` 不起线程；单例在 import 时即读 yaml 构造（「单例构造不读配置」的例外）。只依赖 `app.storage.utils.fs`。

每轮（首轮先等一个 interval）：① `fs.purge_trash` 清回收区；② 扫 `{db_dir}/{task}/{step}/`（两级只认十进制数字名），step 目录自身 `st_mtime` 早于 `now − cleanup_days` 即 `fs.remove` 整个 step（含所有 run），`FAILED` 原样不动、下轮再试；③ `rmdir` 被掏空的数字 task 目录。

- **TTL 从该 step 最后一次开跑算起**：step 的直接子项只有 run 目录，其 mtime = 最近一次 `runs.allocate`；写段 / 写检测不续命，开新 run 续命（`tests/test_storage_cleanup_ttl.py`）。不看 task status（yaml 注释「status=completed」不成立）。
- **被取代的 run 不单独回收**，随 step 留到 TTL。回收与读写不互斥：迟到写 `FileNotFoundError`（recording 记 error 吞掉，离线判 `reclaimed`），锁定读者得 404。
- **活跃 step 不免疫**：连续跑满 `cleanup_days` 的 run 会被删掉正在写的目录。任务超时（health_monitor `task_max_duration`）远短于它，触发不到；但没有启动校验兜这条量级差，调小 `cleanup_days` 或引入长跑任务时要重新评估。
- **数字名过滤是必需的**：否则一个 15 天没动的 `.lab_exports/` 会被当成过期 step 删掉。
- **不复用 `tasks.list_step_ids` / `latest_run_id`**：worker 只删自己扫过的注入根 `db_dir`；`latest_run_id` 与 step mtime 是两个口径。

### 旧布局一律不兼容

读侧只认 `{step}/{run_id}/{domain}/`。更早的 `{step}/hls/` 平铺、微秒命名、共用 `init.mp4`、`.hls_timescale`、`.timeline.idx` / `.idx.json`、`features.jsonl` / `facts.jsonl` / `metadata.json` 等代码无兼容分支，残留随 step TTL 消失。更早结构的清理见 [../update/20260902_LEGACY_LAYOUT_CLEANUP.md](../update/20260902_LEGACY_LAYOUT_CLEANUP.md)。

## 代码来源

- `app/db/{database,tasks,alarms}.py`、`app/types/*.py`、`app/settings.py`（`storage_dir` / `storage_base_dir` / `lab_export_temp_dir`）
- `app/storage/utils/{root,fs}.py`、`app/storage/{tasks,runs}.py`、`app/storage/hls/`、`app/storage/inference/`
- `app/services/run_control/service.py`（`runs.allocate` 唯一调用点）、`app/services/recording/service.py`（hls 与 detections 写者）、`app/services/inference/offline/runner.py`（temporal / label_probs 写者）
- `app/services/inference/online/detection/{infer_proxy,service}.py`（分辨率盖章、写回口、降级帧不落盘）、`app/routers/utils/runs.py`
- `app/services/lab/{clip_builder,step_exporter,runtime_config,service}.py`（`.lab_exports`、临时清单、孤儿扫描）
- `app/daemons/cleanup/`、`config/persistence_config.yaml`
- `tests/test_storage_{tasks,runs,fs,hls,inference,cleanup_ttl}.py`、`tests/test_db_queries.py`
