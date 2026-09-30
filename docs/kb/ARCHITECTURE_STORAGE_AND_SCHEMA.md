> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 存储与 Schema

CleanSight 同时使用平台数据库（只读）和本地文件目录。数据层的准入判据见
[DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)，本文只写现状。

## 数据模型分层（types / ORM / DTO）

运行时数据按来源/生命周期分层，依赖方向单一（types 是叶子，无反向依赖）：

- `app/types/`：跨层共享契约，纯 dataclass / enum，**零服务依赖**（除 numpy 外无框架依赖）。按 concern 分文件：
  `frame.py`(`Frame`) / `detection.py`(`DetBox`,`DetectorOutput`,`FrameDetection`) /
  `temporal.py`(`TemporalEvent`,`TemporalSegment`,`LabelProbs`) / `alarm.py`(`AlarmType`,`AlarmMetric`,`Alarm`) /
  `run.py`(`RunIdentity`) / `exceptions.py`(`AppError` 体系)。标记型 `__init__`，调用方从子模块显式 import。
- `app/db/`：平台 DB 的 ORM 映射，一张表一个模块（`tasks.py` → `DBTask`，`alarms.py` → `DBAlarm`），各带该表的只读 `query_*`（见下文「平台数据库」）。
- DTO：HTTP 请求/响应模型就地定义在各 router，不放 types。
- 不进 types 的：渲染契约 `RenderSpec`/`RenderItem`/`RenderType` 在 `app/services/inference/online/render.py`；
  online 内部传输对象只剩入参 `DetectionTask`（`app/services/inference/online/types.py`），出参直接是 `FrameDetection`。

关键归位：

- `Alarm` 是单一告警抽象：核心字段由产出方（Operator）填，`stage` 由 temporal actor 烧入，`mode` 由
  `online/temporal/alarm_sink.persist_alarms` 在落库边界补，`seq` 由 CQ 闸门 `append_alarm_record_with_gate` 赋。
  `AlarmMetric` 由 Judge/Operator 显式设定，非下游反推。
- run 身份收成 `RunIdentity(task_id, step_id, run_id)`（frozen、按值比较、只含身份不含路径），来源只有
  `app.storage.runs` 的 `allocate` / `query`，调用方不自己构造；CQ 持 `cq.run`。
- **时序契约**（`app/types/temporal.py` 模块 docstring 的三条硬约束）：① `ts`/`start`/`end` 是帧捕获墙钟 ts
  （epoch **浮点秒**，与 `FrameDetection.ts`、HLS `.idx` 同源同值——「对外时间一律 int 毫秒」的具名例外）；
  ② `producer` 是产出者身份唯一真源；③ `meta` 只放伴随观测量，不放被代码读来做判断的键。
  身份键不在形状里，由落盘路径（`RunIdentity`）携带；`type` 判别字段归 storage codec。
  `LabelProbs`（`ts [T]` float64、`probs [T,C]`、`labels`）是可视化旁路，不是事实，不参与任何判断。

### 帧级检测货币 `FrameDetection`（在线写回 / 落盘 / 离线回放同型）

`FrameDetection`（`app/types/detection.py`）是唯一的帧级检测对象：`ts + by_source: Dict[流名, DetectorOutput]`，
外加帧级分辨率 `frame_width` / `frame_height: Optional[int]` 与写回路由句柄 `cq`。检测三粒度一粒度一个名词：
`DetBox`（一个框）→ `DetectorOutput`（一个检测器×一帧，`boxes/metadata/timestamp/success/error`）→ `FrameDetection`（所有检测器×一帧）。

- **分辨率沿每帧轴透传（非检测器输出）**：它是 fan-out 前定死的每帧输入常量，故拆两个显式字段（避免 `(w,h)` 元组隐式序混淆）。
  唯一采集点：`RemoteInferProxy.submit` 从原始帧 `shape` 盖章进 `_Pending.frame_width/height`
  （`app/services/inference/online/detection/infer_proxy.py`）→ collector 组装 `FrameDetection` → 写回口分发 → 落盘/回读随 record 走。缺省 None → 消费方走默认兜底。
- `DetectorOutput.metadata` 只装检测器级数据（如 `model`、失败时的 `error`），检测器不产分辨率。`DetBox.extra` 是保留的单框扩展口，当前零写者、不落盘。
- **`cq` 只在 collector → 写回口这一段有值**：写回口（`online/detection/service.py::_write_back_results`）取走后置 None，留存下来的帧（帧窗 / 快照 / 落盘缓冲 / 离线回读）一律不带。标 `Any` 是因 types 不得依赖 services。
- **在线/离线消费同源**：online `online/temporal/impl/clean.py::_adapt_to_features` 与 offline `offline/impl/clean.py::_collect_object_arrays` 都从 `FrameDetection.frame_width/height` 读取（缺失回退默认尺寸）。两条特征管线仍刻意分离，仅分辨率来源统一。

## 平台数据库（`app/db/`，只读）

后端对平台 DB **只读**，读取只经 `app.db` 的 `query_*`；告警写入走外部 HTTP 上报（见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）。
调用方写 `from app.db import tasks as db_tasks` / `alarms as db_alarms`（`__init__` 标记型、零 re-export）。

- 连接（`app/db/database.py`）：SQLAlchemy `QueuePool`，`pool_pre_ping=True`，常驻 5、溢出 10、回收 3600 秒；连接串来自 `settings.database_url`（`CLEANSIGHT_DB_*` 拼接）。
- **session 生命周期**：每个 `query_*` 自开自关（`with SessionLocal() as s:`），连接查完即还池；返回会话已关闭的 ORM 实例，只能读列属性，不许写。
- **失败语义**：`SQLAlchemyError` 一律包成 `DatabaseError(retryable=True)`（边界层转 503）；降级策略归调用方——timeline 退化成空 events、`/task/history` 的 `source_ip` 置 null、其余 503。构造错误时刻意不传 `task_id=`，否则 `str(exc)` 多出 `[task=…]` 改掉 503 detail。
- 现状只有 routers（api / task / traceback / lab）调 `app.db`；全仓无 router 自开 session。`database.get_db` 仍定义但零调用方。
- 分层门禁：`LAYER_PACKAGES["app/db"] = (app.db, app.types, app.settings)`；`app/storage` 与 `app/db` 互不依赖（`tests/test_import_hygiene.py`）。

### clean_task（`app/db/tasks.py::DBTask`）

关键字段：`_id`（平台主键 varchar）；`task_id`（业务主键 BigInteger，有索引——**运行键即取此**）；`source_ip`（**被动**：诊断 + 遗留 wire 适配，不是路由键）；
`current_step`（字符串，`RunControlService` 边界一次 `int()` 转 step_id）；`status`、`updated_time`、`start_time`、`end_time`。

| 查询 | 语义 |
|---|---|
| `query_task(task_id)` | 按业务主键取一行，无则 None（`/api/start`） |
| `query_source_ips(task_ids)` | 单次 IN 查询 → `{task_id: source_ip}`；空输入直接 `{}`、不开 session |
| `query_task_page(needle, *, limit, offset)` | `(total, rows)`；needle strip 后对 source_ip / status 做 ilike，能转 int 时再 OR task_id；`updated_time desc, task_id desc`（lab db 模式） |

### clean_alarm（`app/db/alarms.py::DBAlarm`）

关键字段：`alarm_id`、`task_id`、`step_id`、`step_name`、`alarm_type`、`severity`、`message`、`detected_at`、`resolved`、`create_time`。

| 查询 | 语义 |
|---|---|
| `query_task_alarms(task_id)` | 该 task 全部告警，`create_time` 降序 |
| `query_step_alarms(task_id, step_id)` | 该 step 告警，`detected_at` 升序 |
| `detected_at_ms(v)` | 按位数把 `detected_at` 归一到毫秒（`<1e11` 秒、`<1e14` 毫秒、其余微秒）；None / ≤0 → `ValidationError`。读 `detected_at` **只能走它** |

## 文件落盘布局

落盘根为 `settings.storage_base_dir`（单一真源，由 `settings.storage_dir` 以项目根为基推导）。数据层
`app/storage/utils/root.py::_storage_root()` 是唯一解析点，记忆化、以 `settings.storage_dir` 原始字符串为缓存 key。

**一次运行（run）一个目录，run 下按域隔离**：

```text
{storage_base_dir}/
  {task_id}/{step_id}/{run_id}/          run 目录；run_id = 分配时刻 epoch 毫秒，同 step 内严格递增
    hls/        {track}_segment_{ts_ms}.mp4   fMP4 fragment，mdhd.timescale pin 死 90000
                {track}_init.mp4              按轨各一份（两轨独立 playlist，不可互指）
                {track}_playlist.m3u8         LIVE 形态，只追加、不写 ENDLIST
                raw_segment_{ts_ms}.idx       raw 轨逐帧 ts sidecar（float64 原值），仅供离线帧反查
                .stage_{track}_{ts_ms}/       insert_segment 写入暂存，commit 后即删
                .clip_*.m3u8 / .export_*.m3u8 lab 临时 VOD 清单（用完即删，不匹配段正则）
    inference/  detections.jsonl / temporal.jsonl / label_probs.npz
                .{name}.tmp                   路线 C 暂存，换名后即消失
  .trash/                                 回收区（utils.fs.remove 的 rename 目标）
  .lab_exports/                           lab 送标 clip 与整段导出的临时件（自带 30 min 孤儿扫描）
  lab_runtime_config.json                 lab 运行时配置（app/services/lab/runtime_config.py）
```

- **盘上不变式**：step 目录的直接子项只有 run 目录（纯数字名）；run 下只有 `DOMAINS = ("hls", "inference")` 域目录、没有文件。存储根下除数字 task 目录与本层回收区 `.trash/` 外，寄居者只剩层外自己拼路径的 `.lab_exports/` 与 `lab_runtime_config.json`（lab 域未建）。
- **定位**（`app/storage/utils/root.py`，包内私有）：`path(task, step)` 逐级定位、不建目录；`run_path(run, domain)` 定位 `{task}/{step}/{run_id}[/{domain}]`；
  `domain_dir(run, domain, *, create)` 是各域文件的统一入口，非 `RunIdentity` 即 `TypeError`、域名不在白名单即 `ValueError`（均早于建目录），`create=True` **只建域这一级**（`fs.ensure_dir`，不带 parents）。
- **run 目录只由 `runs.allocate` 建**（全层唯一 `mkdir(parents=True)` 出产物目录的地方）。写者不建 run 目录：run 被回收后的迟到写在 `domain_dir` / `fs.replace` 处 `FileNotFoundError`，不会重建出僵尸目录。
- **盘上原语**（`app/storage/utils/fs.py`，全层唯一一份）：`replace(path, write_fn)`（同目录 `.{name}.tmp` → `os.replace`，异常先删 tmp 再上抛，不建父目录）；
  `remove(path, *, root)`（rename 进 `{root}/.trash/{uuid}` 再 rmtree，三态 `ABSENT` / `REMOVED` / `FAILED`，失败只记 warning 不抛）；`purge_trash`；`ensure_dir`。包外唯一调用方是 `app/daemons/cleanup/worker.py`（只用 `remove` / `purge_trash`）。
- 时间量纲：`run_id`、段名 / `.idx` 名 / stage 目录名 / `.m3u8` 段 URI 里的 `ts_ms` 全是 int 毫秒；inference 域三份产物与 `.idx` **内容**里的 ts 仍是浮点秒（`Frame.timestamp` 派生）。

### run：分配、查询、可见性（`app/storage/runs.py`）

| 成员 | 语义 |
|---|---|
| `allocate(task, step)` | `run_id = max(time_ns // 1_000_000, 已有最大 + 1)`（时钟停滞 / 回拨也严格递增），再 `mkdir(parents=True)`；不带 `exist_ok`——目录已在说明分配没串行，抛 `OSError`。调用方须持 `lock_for` |
| `query(task, step, run_id=None)` | 给了 `run_id`：目录在就返回（不做可见判断），不在 → None。缺省：按 run_id 降序返回第一个**可见** run，都不可见 → None |
| 可见判据 `_visible` | `inference/detections.jsonl` 存在，或任一轨 `hls.query_has_segments`（清单里有已登记段）。只影响缺省查询 |
| `successor(run)` | 同 step 下紧接着分配的 run_id，不看可见性；最新 → None |
| `query_latest_by_step(task)` | 各 step 的最新可见 run，按 step 升序；无可见 run 的 step 不出现 |
| `query_lifespan_ms(run)` | `[run.run_id, successor)` 墙钟毫秒；最新 run 上界 None |

- 唯一分配调用点：`RunControlService.start_run`（`app/services/run_control/service.py`），在 `client_service.lock_for(task_id)` 内、早于 CQ 构造与 `client_service.set`；`OSError` 包成 `AppError`（500）。分配出的 run 目录在起流失败时不回收（空目录随 step TTL 清）。
- **换代不删旧产物**：同 step 重启 = 分配新 run 写新目录，旧 run 保留到 step TTL，可按 `run_id` 点名回放 / 离线分析。
- 已知窗口：新 run 因 `detections.jsonl`（~1 s）先于首段 hls（10 s+）可见，这期间不带 `run_id` 的回放返回 404 / 空，不回落上一次录像（接受的行为）。
- 读侧入口统一经 `app/routers/utils/runs.py`（`resolve_run` / `resolve_timeline` / `resolve_media_run` / `no_run`）解析一次 run，此后整次请求只读这个 run；MediaToken payload 的可选键 `"r"` 把 run 锁进段 URL。接线细节见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)。

### `hls/`：播放产物

`processed` 轨不产 `.idx`——渲染后的帧只用于展示，离线不消费。`.idx` 与段同名同目录、一帧一条 float64 ts 原值，
在 commit 链首位落盘（`sidecar → init → 段文件 → 清单条目`）；格式、写读顺序与「ts 位级精确」契约见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)。

**写者唯一**：`app/services/recording/service.py`（`hls.insert_segment(run, …)`）；层外另有 lab 往 `hls/` 放用完即删的临时 VOD 清单（`init_path(run, track).parent`）。
**读者**：`app/routers/{media,task,traceback,lab}.py`、`app/routers/utils/runs.py`（`resolve_timeline`，ai / lab 经它）、
`app/services/lab/{clip_builder,step_exporter}.py`、`app/storage/runs.py`（可见判据）——全部经 `app.storage.hls`，业务层不拼路径。
`app/services/utils/media_timeline.py` 只 import `MediaTimeline` 类型做断流判定，不碰盘。

对外面（`app/storage/hls/__init__.py` 的 `__all__`；所有定位 / 读写口首参 `run: RunIdentity`）：

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

- 段身份键 `SegmentRef(track, ts_ms)`（不带 task/step/run）；`ts_to_ms(ts) = floor(Fraction(ts) * 1000)`——按 float 精确值向下取整，不是 `int(ts*1000)`（乘法先舍入：`0.29*1000 == 290.0` 而 0.29 精确值略小），保证「段名 ≤ 首帧」在精确值、`ts_ms/1000`、`ts*1000` 三种口径下都成立（`tests/test_storage_hls.py`）。
- `HlsSpan(tracks, start_ms, last_start_ms, end_ms)`，`end_ms = max(ts_ms + round(EXTINF×1000))`。
- **「有哪些段」只由 `{track}_playlist.m3u8` 回答**：文件系统枚举已从域里删除。盘上有文件但清单没条目 = 不是段（在途段或登记失败的段），故容器叫 `Segment`、枚举叫 `list_segments`。
- hls 域不存统计文件：段数 / 时长走 `query_span`，run 的 hls 可见判据是「清单里有段」。

### `inference/`：推理产物

`app/storage/inference/`（facade `__init__` + `_layout` / `_jsonl` / `_detection` / `_temporal`），按**产出层**分模块：
`_detection` 管检测层（L1）产物，`_temporal` 管时序层（L3）产物与逐帧概率。对外 7 个成员全部只收 `RunIdentity`：
`append_detections` / `read_detections` / `read_temporal` / `write_temporal` / `read_label_probs` / `write_label_probs` / `query_has_offline_results`。
刻意没有：`append_temporal`（`TemporalEvent` 零生产者）、`iter_detections`（无流式消费方）、`*_path`（层外不取载荷字节）、任何删除成员（run 目录回收归 cleanup daemon）。本域不持锁，同一 run 的写由调用侧串行。

| 产物 | 货币 | 写路线 | 写者 | 读者 |
|---|---|---|---|---|
| `detections.jsonl` | `FrameDetection` | B 追加（一次 `open("a")`，包内不攒批；空批 no-op 且不建目录） | `RecordingService._write_detections`（recording 的 detections 队列线程） | `OfflineRunner`（`read_detections`）；`runs` 可见判据（文件存在即可见） |
| `temporal.jsonl` | `TemporalEvent \| TemporalSegment` | C 原子整体替换（`fs.replace`）；空序列照写空文件 | `OfflineRunner._replace_segments`（离线子进程，唯一写者） | `POST /ai/temporal`（只取 Segment）、`offline.cli query`、`query_has_offline_results` |
| `label_probs.npz` | `LabelProbs` | C 原子整体替换（`np.savez` 写文件对象，避免自动补后缀） | `OfflineRunner._maybe_write_label_probs` | `POST /lab-f3m8/label-probs`、`query_has_offline_results` |

**`detections.jsonl` record**（`_detection.py` 的 `_frame_to_record` / `_record_to_frame`，一对逆运算紧挨放置）：

```json
{"ts": <float>, "detections": {"<流名>": [{"bbox": [x1,y1,x2,y2], "conf": <float>, "cls_id": <int>, "cls": "<class_name>"}, ...], ...}, "frame_width": <int>, "frame_height": <int>}
```

- `ts` = 帧捕获时间戳（`FrameDetection.ts`），与在线滑窗、HLS raw `.idx` 逐帧 ts 同源同值，可按 ts 精确对上同帧录像。反序列化边界统一 `float`。
- 每框只落 `bbox/conf/cls_id/cls`；**刻意不落** `DetBox.extra`、`metadata`、`success/error`（回读按契约默认还原）。含空框列表的 source 照落——「该流没检出」与「没有该流」是两回事。
- `frame_width/frame_height` 仅当两者皆非 None 时落顶层，回读还原到 `FrameDetection` 字段；全链路命名键、无位置约定。
- 降级帧（任一源 `success=False`）不进落盘缓冲（`online/detection/service.py`）：落盘格式不带 `success`，空框会被离线当成「没检出」；失败时段在离线侧是时间空洞，整 run 全失败则离线 `skipped`。
- 写侧不吞异常：`OSError` 原样抛，由 `SerialTaskQueue` 记 error；**失败不重试**（纯追加，重试会写重复帧）。
- 回读 `read_detections(run)`：单次顺序扫文件、按 ts 升序（契约）；文件缺失返回 `[]`，坏行 / 形状不对的 record 跳过 + warning；JSONL 读用 `utf-8-sig` 容忍 BOM。
- 每个 run 一个新目录，文件天然从空开始、不截断不认领；格式演进不做迁移。

**`temporal.jsonl` / `label_probs.npz`**：

- `temporal.jsonl` 每条一行、`type: "event"|"segment"` 判别 + 全字段无损；`read_temporal` **不排序**（两型无共同时间键），坏行跳过 + warning。
- `write_temporal` 是整体替换，盲写会吃掉别人的事实，调用方须 read → 合并 → write。现行合并规则（`offline/runner.py::_replace_segments`）：丢**全部**旧 `TemporalSegment`、保留 `TemporalEvent`（一个 stage 至多一个离线模型）。当前只有离线一个写者，同 run 跨进程并发跑离线无互斥，「该 run 正在运行」由作业服务在提交时 409 挡。
- `label_probs.npz`：键 `ts` float64（无损）、`probs` **float16**（有损，读回 float32）、`labels` unicode；读写 `allow_pickle=False`；不存在返 None，损坏直接抛。Runner 先写它、后写 `temporal.jsonl`（**旁路先于事实**：事实落盘即表示本次运行完成）。
- `query_has_offline_results(run)`：`temporal.jsonl` 里有 `TemporalSegment`，或 `label_probs.npz` 存在（只判存在不解析）；只有 `TemporalEvent` 不算。消费方：lab 任务列表的 `offline_steps`。

### 跨域：`tasks.py`

`app/storage/tasks.py` 回答「有哪些 task / step」，不出定位能力，**没有删除成员**：

| 成员 | 语义 |
|---|---|
| `list_task_ids(order="id"\|"recent")` | 存储根下的数字 task 目录；`"recent"` 按 `latest_run_id` 降序，同值 task_id 大者优先；无 run 目录的 task 键为 0 排最后但仍在结果里 |
| `latest_run_id(task)` | 该 task 所有 run 目录的最大 run_id（不看产物）；是任何「最新可见 run」的上界，只配剪枝 |
| `list_step_ids(task)` | 该 task 下的 step id，**升序，不过滤空 step**——「里面有没有产物」是域知识，由调用方问对应的域 |

「最近活动」= 最大 `run_id`（目录名即开跑时刻），不读 mtime、不下钻。对外时间一律取 `HlsSpan` 毫秒值。

### 读侧查询（`query_*`）清单

从盘上事实推出一个答案（句柄 / 布尔 / 汇总）的成员，与 `list_*` / `read_*` 并存：

| 成员 | 返回 | 消费方 |
|---|---|---|
| `runs.query` | `Optional[RunIdentity]` | `routers/utils/runs.py`、`offline/runner.py`、`offline/cli.py` |
| `runs.query_latest_by_step` | `List[RunIdentity]` | `routers/task.py`（history 深扫）、`routers/lab.py`（storage 模式任务列表） |
| `runs.query_lifespan_ms` | `(start_ms, Optional[end_ms])` | `routers/traceback.py`（timeline 告警按 run 存续期过滤） |
| `hls.query_span` | `Optional[HlsSpan]` | `routers/{task,traceback,lab}.py` |
| `hls.query_has_segments` / `query_has_init` | `bool` | `runs` 可见判据、lab 列表；traceback playlist |
| `hls.query_timeline` | `MediaTimeline` | `routers/traceback.py`、`routers/utils/runs.py::resolve_timeline`、`services/lab/clip_builder.py` |
| `inference.query_has_offline_results` | `bool` | `routers/lab.py` |

### TTL 回收与回收区

实现在 `app/daemons/cleanup/`：`worker.py::CleanupWorker`、`instance.py::cleanup_worker`、`config.py::CleanupConfig`（读
`config/persistence_config.yaml` 的 `storage:` 段：`enable_cleanup`（代码缺省 False、yaml true）/ `cleanup_days`（代码缺省 7、yaml 15）/
`cleanup_interval_seconds`（3600）；扫描根委托 `settings.storage_base_dir`）。`lifespan()` 在 `enable_cleanup` 为假时不起线程；
单例在 import 时即读 yaml 构造（单例「构造不读配置」的例外）。只依赖 `app.storage.utils.fs`，不 import 任何 services。

每轮（`_scan_and_clean`，首轮等一个 interval）：

1. `fs.purge_trash(root=db_dir)` 清空回收区（上一轮 rmtree 没删掉的残留）。
2. 扫 `{db_dir}/{task}/{step}/`（**两级只认十进制数字目录名**），`{step}/` 目录自身 `st_mtime` 早于 `now − cleanup_days` 即 `fs.remove(step_dir, root=db_dir)` 整个删（含其下所有 run）。只有 `REMOVED` 计数；`FAILED`（rename 失败）盘上原样不动，下一轮再试。
3. `rmdir` 被掏空的数字 task 目录。

判据与边界：

- **判据 = `{step}/` 目录自身的 mtime，不下钻。** step 的直接子项只有 run 目录，目录 mtime 只在增删直接子项时变，故它 = 最近一次 `runs.allocate`：**TTL 从该 step 最后一次开跑算起**，写段 / 写检测不续命，分配新 run 续命（`tests/test_storage_cleanup_ttl.py`）。判据不看 task status（yaml 注释「status=completed」不成立）。
- **不单独回收被取代的 run**：随 step 留到 TTL，回放、离线锁定的旧 run 在此之前一直可用，无需宽限期。回收不看 run、不看可见性，与写侧 / 读侧都不互斥：写者不建 run 目录，回收后的迟到写原子失败（`FileNotFoundError`，recording 队列记 error 吞掉、离线判 `reclaimed`），锁定读者得到 404。
- **删除原子**：rename 进 `.trash/` 后再 rmtree，不会出现「删了一半的目录里留着新文件」；`root` 与 step 同卷。
- **活跃 step 不免疫**：连续跑满 `cleanup_days` 的 run 会被删掉正在写的目录。任务超时远短于 `cleanup_days`，触发不到；调小 `cleanup_days` 或引入长跑任务时要重新评估。
- **数字名过滤是必需的**：存储根下的 `.trash/`、`.lab_exports/` 都不是数字名；不挡的话一个 15 天没动过的导出临时目录会被当成过期 step 删掉。`.lab_exports/` 由 lab 自带 30 min 孤儿扫描。
- **不复用 `tasks.list_step_ids` / `latest_run_id`**：worker 扫注入的 `db_dir`，不让删除动作认一个它自己没扫过的根；且 `latest_run_id` 答「最近开跑」，与 step mtime 是两个口径。

### 旧布局一律不兼容

读侧只认 `{step}/{run_id}/{domain}/`。更早的 `{step}/hls/`、`{step}/inference/`、step 根平铺、微秒命名、共用 `init.mp4`、`.hls_timescale` 缓存、
`.timeline.idx`/`.idx.json` 两代 sidecar、`features.jsonl` / `facts.jsonl` / `metadata.json` 等**不做兼容、代码无兼容分支**，残留文件无读写方、随 step TTL 自然消失。
更早结构的清理见 [../update/20260902_LEGACY_LAYOUT_CLEANUP.md](../update/20260902_LEGACY_LAYOUT_CLEANUP.md)。

## 代码来源

- `app/db/{database,tasks,alarms}.py`
- `app/types/{frame,detection,temporal,alarm,run,exceptions}.py`
- `app/settings.py`（`storage_dir` / `storage_base_dir` 单一真源、`lab_export_temp_dir`）
- `app/storage/utils/root.py`（`DOMAINS` 白名单、`path` / `run_path` / `domain_dir` / `run_ids`、根解析记忆化）
- `app/storage/utils/fs.py`（`replace` / `remove` / `purge_trash` / `ensure_dir`）
- `app/storage/{tasks,runs}.py`
- `app/storage/hls/`（`__init__` 对外面、`_layout` 命名与 `ts_to_ms`、`_read` 清单枚举与 `query_*`、`_write` 事务、`_idx`、`_timeline` 媒体轴、`types`）
- `app/storage/inference/`（`_layout` / `_jsonl` / `_detection` / `_temporal`）
- `app/services/run_control/service.py`（`runs.allocate` 唯一调用点）
- `app/services/recording/service.py`（hls 唯一写者、detections 写者）
- `app/services/inference/offline/runner.py`（temporal / label_probs 写者）
- `app/services/inference/online/detection/{infer_proxy,service}.py`（分辨率盖章 / 写回口分发、降级帧不落盘）
- `app/routers/utils/runs.py`（读侧 run 解析）
- `app/services/lab/{clip_builder,step_exporter,runtime_config}.py`（`.lab_exports` / 临时清单 / `lab_runtime_config.json` 的落点）
- `app/daemons/cleanup/{worker,config,instance,__init__}.py`、`config/persistence_config.yaml`（`storage:` 段）
- `tests/test_storage_{tasks,runs,fs,hls,inference,cleanup_ttl}.py`、`tests/test_db_queries.py`
