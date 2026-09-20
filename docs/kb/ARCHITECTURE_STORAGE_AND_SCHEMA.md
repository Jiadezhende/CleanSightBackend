> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 存储与 Schema

CleanSight 同时使用数据库表和本地文件目录。

## 数据模型分层（domain / ORM / DTO）

运行时数据按来源/生命周期分层，依赖方向单一（domain 是叶子，无反向依赖）：

- `app/domain/`：跨服务共享契约，纯 dataclass，**零服务依赖**。按 concern 分文件：`frame.py`(`Frame`) / `detection.py`(`Detection`,`FrameDetections`,`FrameFeature`) / `alarm.py`(`AlarmType`,`AlarmMetric`,`Alarm`) / `render.py`(`RenderSpec`,`RenderItem`,`RenderType`)。调用方从子模块显式 import。
- `app/models.py`：仅 ORM 行对象 `DBTask`/`DBAlarm`。
- DTO：HTTP 请求/响应模型就地定义在各 router，不放 domain。
- inference 内部件（`FrameInference`/`DetectionTask`/`EventFact`/`SegmentFact`）留在 `inference/models.py`，不入 domain（pipeline-internal）。

关键归位：`Alarm` 吸收旧 `AlarmRecord`（单一告警抽象，`mode/stage/seq` 由 `alarm_sink` 在落库边界补齐）；`AlarmMetric` 由 Judge/Operator 显式设定，非下游反推；`app/domain/` 无 `task.py`，身份 primitives（task_id/step_id）直挂 ClientQueues。

### 帧级特征货币 `FrameFeature`（在线滑窗 / 离线回放同源）

`FrameFeature`（`app/domain/detection.py`）是特征层输入货币：一帧多流对齐的检测记录 `ts + by_source: Dict[流名, FrameDetections]`，外加**帧级分辨率** `frame_width: Optional[int]` / `frame_height: Optional[int]`。在线写回口物化、离线回放重建，两端同型。

- **分辨率沿每帧轴透传（非检测器输出）**：`frame_shape`（帧分辨率）是 fan-out 前定死的每帧输入常量（一个 `DetectionTask` 扇给 stage 内 N 个模型，各流看同一张图），故拆两个显式字段（避免 `(w,h)` 元组隐式序混淆，本仓库有 frame_shape(H,W,C)/wh(W,H)/frame_width 名义打架前例）。采集链：pool（`detection/pool.py`）从原始帧盖章 `frame_width=frame.shape[1], frame_height=frame.shape[0]`（唯一采集点，原始帧此后即销毁）→ `FrameInference`（`inference/models.py` 同名字段）随传输消息透传 → 写回口（`detection/service.py`）物化进 `FrameFeature` → 落盘/回读随 record 走。缺省 None → 消费方走默认兜底。
- **不再走 `FrameDetections.metadata`**：`FrameDetections` 结构不动，`metadata` 只装真·检测器级数据（`model`/`error`/`mean_brightness`）；检测器（`detection/detector.py`、`detection/impl/mock.py`）不产 `frame_shape`。
- **在线/离线消费同源**：online `temporal/impl/clean.py._adapt_to_features` 与 offline `offline/impl/clean.py` 都从 `FrameFeature.frame_width/height` 读取（缺失该帧留全零行 / 回退默认尺寸）。两条特征管线（online 6 维、offline 113+ 维）仍刻意分离，仅分辨率来源统一。

## 数据库连接

数据库连接由 `app/database.py` 创建：

- SQLAlchemy `QueuePool`
- `pool_pre_ping=True`
- 常驻连接数 5
- 最大溢出连接 10
- 连接回收 3600 秒

连接字符串来自 `settings.database_url`，由 `CLEANSIGHT_DB_*` 环境变量拼接。

## clean_task

ORM：`DBTask` in `app/models.py`

关键字段：

- `_id`：平台主键，varchar。
- `task_id`：业务主键，BigInteger，有索引——**运行键即取此**（int）。
- `source_ip`：客户端来源字段，**被动**（诊断 + 遗留 wire 适配），不再是路由键。
- `current_step`：当前步骤，字符串（RunController 边界一次 `int()` 转 step_id）。
- `status`、`updated_time`、`start_time`、`end_time`。

## clean_alarm

ORM：`DBAlarm` in `app/models.py`

关键字段：

- `alarm_id`
- `task_id`
- `step_id`
- `step_name`
- `alarm_type`
- `severity`
- `message`
- `detected_at`
- `resolved`
- `create_time`

查询接口主要从 DB 读取；告警写入当前代码通过外部 HTTP 上报接口完成。

## 文件落盘布局

落盘根目录为 `settings.storage_base_dir`（单一真源，由 `settings.storage_dir` 以项目根为基
推导）。数据层 `app/storage/_root._storage_root()` 是唯一解析点，记忆化、以
`settings.storage_dir` 原始字符串为缓存 key。

产物按**域子目录**隔离在 step 之下：

```text
{storage_base_dir}/{task_id}/{step_id}/{hls|features|lab}/
```

域名取自 `app/storage/_root.DOMAINS = ("hls", "features", "lab")` 白名单，拼错当场
`ValueError`——不会静默造出第四个子目录。定位统一走
`_root.path(task_id, step_id, domain, *, create=False)`：三个位置参数从左到右逐级下钻，省到
哪一级返回哪一级；跳级（给了深层省了浅层）或域名不在白名单 → `ValueError`。

**三个域里只有 `hls` 有生产写者**。`features` / `lab` 的白名单条目与（部分）数据层代码已就
位，但产物仍落在旧位置，见下方「未迁入的两个域」。

### `hls/`：调用点已全部迁入（2026-09-16）

```text
{storage_base_dir}/{task_id}/{step_id}/hls/
  raw_init.mp4                    # fMP4 init，raw 轨专用（首段转码时产出、整条 playlist 复用）
  processed_init.mp4              # 同上，processed 轨专用——两轨独立 playlist，不可互指
  raw_segment_{ts_us}.mp4         # fMP4 fragment，mdhd.timescale pin 死 90000
  processed_segment_{ts_us}.mp4
  raw_segment_{ts_us}.idx         # raw 逐帧 ts sidecar（float64 数组），仅供离线帧反查
  raw_playlist.m3u8               # LIVE 形态，只追加、不写 ENDLIST
  processed_playlist.m3u8
  metadata.json                   # 段数 / 时长 / 首末 ts 统计
  .stage_{track}_{ts_us}/         # 写入事务的暂存目录，commit 后即删
```

`processed` 轨不产 `.idx`——渲染后的帧只用于展示，离线推理不消费它。`.idx` 与段同名同目录、
一帧一条 float64 ts 原值，**先于同名 mp4 落盘**；格式、写读顺序与「ts 位级精确」契约见
[DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)。

**写者唯一**：`app/services/recording/service.py`（`hls.insert_segment` / `hls.delete`）。
**读者**：`app/routers/{media,task,traceback,lab}.py`、`app/services/lab/{clip_builder,step_exporter}.py`、
`app/services/inference/offline/frame_tracker.py`、`app/services/utils/media_timeline.py`——
全部经 `app.storage.hls`，业务层一个路径字符串都不拼。

对外面（`app/storage/hls/__init__.py` 的 `__all__`）：

```text
写      insert_segment / delete
读帧    read_segment / iter_frames                （只服务 raw 轨，processed 不落 sidecar）
枚举    list_segments / list_segments_in_range
定位    segment_path / init_path / sidecar_path / playlist_path
命名    segment_name / init_name / parse_segment_name / parse_init_name / ts_to_us
形状    Segment / SegmentRef / TRACKS
```

**「有哪些段」只由 `{track}_playlist.m3u8` 回答**（2026-09-19 收口）：文件系统枚举
（`iterdir` + 文件名正则）曾是并行的第二个入口，**已整个从域里删除**，不是降级为私有。盘上
有文件但清单没条目 = 不是段——可能是在途段，也可能是登记失败的段，两种喂给下游都是 hls.js
缓冲洞或 ffmpeg 静默截短。收口后「可播」不再是限定词，故容器叫 `Segment`、枚举叫
`list_segments`（旧名 `PlayableSegment` / `list_playable_segments` 已不存在）。

### 未迁入的两个域：features / lab 的产物仍在旧位置

```text
{storage_base_dir}/
  {task_id}/{step_id}/features.jsonl   # L2 检测特征，落在 step 根，不是 {step}/features/
  {task_id}/{step_id}/facts.jsonl      # L3 事实账本，同上
  .lab_exports/                        # 送标 clip 与整段导出的临时件
  lab_runtime_config.json              # lab 运行时配置（任务列表数据源等）
```

- **features 写侧未切**：`app/storage/feature.py` 已落地（`append_features` / `read_features` /
  `delete_features` 三个成员，写 `{step}/features/features.jsonl`），但**生产零调用点**——
  `app/services/inference/feature/store.py` 的 `_JsonlBuffer._path` 仍是
  `{base}/{task_id}/{step_id}/{suffix}.jsonl`，`FeatureStore` 与 `FactLedger` 都由它出路径。
- **`app/storage/lab.py` 不存在**：`app/storage/` 下只有 `_root.py` / `tasks.py` / `feature.py` /
  `hls/`。lab 的临时件根由 `settings.lab_export_temp_dir` 给，空则
  `{storage_base_dir}/.lab_exports`，`ClipBuilder` 与 `StepExporter` 各自 `Path` 拼；
  `lab_runtime_config.json` 由 `app/services/lab/config.py` 直接落在存储根。
- 故数据层那条「step 根下只有域目录、没有文件；存储根下只有数字命名的 task 目录」的不变式，
  目前**只在 `hls` 这一个域上成立**。它是 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)
  的准入判据，不是当前盘上事实。

### 跨域枚举与删除

`app/storage/tasks.py` 是本层唯一的跨域模块，三个成员都以 `(task_id, ...)` 开头：

| 成员 | 语义 |
|---|---|
| `list_task_ids(order="id"\|"mtime")` | 存储根下的数字命名 task 目录；`"mtime"` 按「最后有人写东西」降序 |
| `list_step_ids(task_id)` | 该 task 下的 step id，**升序，不过滤空 step** |
| `delete_step(task_id, step_id)` | rmtree 整个 step 目录（含**所有写者**的产物），父 task 目录若因此变空一并回收 |

- `list_step_ids` 不过滤空 step 是刻意的：「里面有没有产物」是域知识，要按产物过滤的调用方自
  己去问对应的域（`hls.list_segments` 等）；而 TTL 要的正是没过滤的那一档。
- `order="mtime"` **必须下钻域子目录取最大值**：产物落进 `{step}/{domain}/` 之后，写一个段只
  更新 `hls/` 的 mtime，`{step}/` 本身纹丝不动（它只在新建域目录那一刻变）。不下钻会让「最近
  活动」静默退化成「首次落盘」。它是近似值、只配挑深扫候选，对外时间一律取真实段 ts。
- **TTL 判据是 `{step}/` 目录自身的 mtime**（`app/services/persistence/workers/cleanup_worker.py`），
  **不再读 `metadata.json`**：域隔离后 `metadata.json` 挪进 `{step}/hls/`，旧的
  `glob("*/*/metadata.json")` 匹配不到它，表现是新数据永不回收、老数据照常回收——单向漏盘且无
  任何日志。目录 mtime 对平铺与分域两种布局一视同仁。反过来，`{step}/` 的 mtime 恰好是「创建
  时间」的好代理，故它与 `list_task_ids(order="mtime")` 是两个口径、不能复用同一个函数。

### 旧布局一律不兼容

读侧**只认 `{step}/hls/`，不回落旧平铺布局**：升级前落在 `{step}/` 的段在域看来不存在，随
TTL 自然消失。这是刻意决策，不是遗漏。更早的结构（共用 `init.mp4`、`.hls_timescale` 缓存、
`.timeline.idx`/`.idx.json` 两代 sidecar）同样**不做兼容、无迁移路径**，见
[../update/20260902_LEGACY_LAYOUT_CLEANUP.md](../update/20260902_LEGACY_LAYOUT_CLEANUP.md)。

### 退役但尚未删除的旧读写侧

三处代码仍在仓库里，读写的都是旧平铺布局，**接上去只会读到空**：

- `app/services/persistence/strategies/hls_strategy.py`（旧写侧）：`persistence/manager.py`
  **构造但不启动**，`start()` 不再碰它。重新启用 = 与 recording 双写同一 step，两端都不报错，
  产出的是时间轴各自自洽却互相错位的段。它的 docstring 仍自称「step 目录落盘格式的唯一写侧
  真源」并描述平铺布局，**那已是历史文本**。
- `app/services/traceback/segment_finder.py` 的 `SegmentFinder`：**无现役调用点**。
  `traceback/__init__.py` 刻意不 re-export `SegmentFinder` / `SegmentRef` / `StepRef`，防残留
  引用静默拿到旧型。
- `app/services/inference/offline/frame_tracker.py` 的 `Timeline` 类：同样无现役调用点，能力已
  由 `hls.iter_frames` 取代，同文件的 `FrameTracker` 已改调数据层。注意该模块顶部仍有
  `from app.services.traceback.segment_finder import SegmentFinder, SegmentRef, get_default_base_dir`
  ——那句只服务于已退役的 `Timeline`，所以准确说法是「无现役调用点」，不是「已无任何引用」。

## FeatureStore 落盘 record（features.jsonl）

`FeatureStore`（`app/services/inference/feature/store.py`，继承 `_JsonlBuffer`）按 `(task_id, step_id)` 缓冲批量追加写 `features.jsonl`，常开、best-effort（IO 异常只记日志不抛）。`append`/`load` 两端货币均为帧级 `FrameFeature`；磁盘 record 是它的**精简投影**，一对逆运算 `_feature_to_record`/`_record_to_feature`（紧挨放置、互为逆）。

**落盘路径是 `{storage_base_dir}/{task_id}/{step_id}/features.jsonl`（step 根，平铺）**，由
`_JsonlBuffer._path` 拼；`base_dir` 在 `InferenceManager` 构造时注入并可 `configure()` 改。
同一底座的 `FactLedger` 以 `suffix="facts"` 落同目录的 `facts.jsonl`。数据层的
`app/storage/feature.py`（写 `{step}/features/features.jsonl`）是这份逻辑的目标归宿，**目前
只有测试在用**，`store.py` 未改调它——两者的 `_feature_to_record` / `_record_to_feature`
是各自一份、逐字段同形的拷贝。

每行一条 record，形状：

```json
{"ts": <float>, "features": {"<流名>": [{"bbox": [x1,y1,x2,y2], "conf": <float>, "cls_id": <int>, "cls": "<class_name>"}, ...], ...}, "frame_width": <int>, "frame_height": <int>}
```

- **`ts`**：帧捕获时间戳，= 该帧 `FrameFeature.ts`，与在线滑窗、HLS 段/keypoints 的 `fd.timestamp` 同源同值，故 feature 行可按 `ts` 精确对上同帧 HLS 证据。反序列化边界统一 `float`（手写 JSONL 可能给 int）。
- **`features`**：`{流名: [检测框...]}`，每框只落 `bbox/conf/cls_id/cls`。**刻意不落**（回读按契约默认还原）：`mask`/`keypoints`（重，seg/pose 才有，离线不消费）、`metadata`/`success`/`error`（离线不消费）。
- **`frame_width`/`frame_height`（帧级分辨率，全命名键）**：仅当两者皆非 None 时落顶层；缺一即整体省略。回读还原到 **`FrameFeature.frame_width/height` 字段**，`FrameDetections.metadata` 保持为空 `{}`。全链路命名键、无位置约定。
- **回读**（`load(task_id, step_id) -> List[FrameFeature]`）：单次顺序扫文件，每行 `_record_to_feature` 还原一个 `FrameFeature`（by_source 含该行全部 source，含 detections 为空的 source），按 `ts` 升序；文件缺失返回 `[]`，单行损坏记 warning 跳过。回读用 `utf-8-sig` 容忍 Windows 手写文件的 UTF-8 BOM。

> **兼容与迁移**：`features.jsonl` 是随 step 目录 TTL 回收的临时件、每 run 重生（`open_fresh` 起始截断分区），故格式演进不做迁移。极端情况（run 跨部署，旧 `wh` 位置数组行被新码读）→ 该行分辨率缺失 → offline 默认兜底（无 crash，仅该 step 用默认尺寸归一化）。旧 `features.jsonl` 因命名键缺省即省略而天然兼容。

同目录还有 `facts.jsonl`：`FactLedger`（同 `_JsonlBuffer` 底座）落 L3 `EventFact`/`SegmentFact`，online 链路不再写，供离线回读（`replace_segments` 支持按 producer 幂等替换分段）。

## Metadata

`metadata.json` 落在 `{step}/hls/`（随域隔离一起挪的），由 `app/storage/hls/_meta.py` 读改写
（整份 tmp + `os.replace` 换名，路线 C）。内容：task/step、raw/processed 两轨各自的段数量、
总时长、首尾时间戳，外加 `created_at` / `updated_at`。

两条限定：

- **它是派生量不是真值**——段时长的真值在 playlist 的 `#EXTINF` 里，读侧要真时长走
  `hls.list_segments`。
- `end_time` 从来只写 `null`（没有哪个写侧知道 step 何时结束），保留只因读侧还按这个形状解析。
  `updated_at` 曾是 TTL 判据，现已改为目录 mtime，详见上文「跨域枚举与删除」。

## 代码来源

- `app/database.py`
- `app/models.py`
- `app/domain/{frame,detection,alarm,render}.py`
- `app/settings.py`（`storage_dir` / `storage_base_dir` 单一真源、`lab_export_temp_dir`）
- `app/storage/_root.py`（`DOMAINS` 白名单、`path()` 逐级下钻、根解析记忆化）
- `app/storage/tasks.py`（`list_task_ids` / `list_step_ids` / `delete_step`）
- `app/storage/hls/`（`__init__` 的对外面、`_layout` 文件名、`_read` 清单枚举、`_write` 事务、`_meta` metadata.json）
- `app/services/recording/service.py`（hls 域唯一写者）
- `app/services/persistence/workers/cleanup_worker.py`（TTL 判据 = step 目录 mtime）
- `app/services/inference/feature/store.py`（`features.jsonl` / `facts.jsonl` 的**现役**写读侧，仍是平铺路径）
- `app/storage/feature.py`（features 域的目标实现，生产零调用点）
- `app/services/lab/{clip_builder,step_exporter,config}.py`（`.lab_exports` 与 `lab_runtime_config.json` 的落点）
- `app/services/inference/models.py`（`FrameInference` 帧级 wh 透传）
- `app/services/inference/detection/{pool,service,detector}.py`（wh 盖章 / 物化 / 停产 frame_shape）
- `config/persistence_config.yaml`

