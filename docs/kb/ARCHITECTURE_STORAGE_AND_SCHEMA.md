> 更新时间：2026-07-21
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 存储与 Schema

CleanSight 同时使用数据库表和本地 HLS 文件目录。

## 数据模型分层（domain / ORM / DTO）

运行时数据按来源/生命周期分层，依赖方向单一（domain 是叶子，无反向依赖）：

- `app/domain/`：跨服务共享契约，纯 dataclass，**零服务依赖**。按 concern 分文件：`frame.py`(`Frame`) / `detection.py`(`Detection`,`FrameDetections`,`FrameFeature`) / `alarm.py`(`AlarmType`,`AlarmMetric`,`Alarm`) / `render.py`(`RenderSpec`,`RenderItem`,`RenderType`)。调用方从子模块显式 import。
- `app/models.py`：仅 ORM 行对象 `DBTask`/`DBAlarm`。
- DTO：HTTP 请求/响应模型就地定义在各 router，不放 domain。
- inference 内部件（`FrameInference`/`DetectionTask`/`EventFact`/`SegmentFact`）留在 `inference/models.py`，不入 domain（pipeline-internal）。

关键归位：`Alarm` 吸收旧 `AlarmRecord`（单一告警抽象，`mode/stage/seq` 由 `alarm_sink` 在落库边界补齐）；`AlarmMetric` 由 Judge/Operator 显式设定，非下游反推；旧运行时 `CleaningTask` VO（曾 Pydantic）已删（`app/domain/` 已无 `task.py`），身份 primitives（task_id/step_id）直挂 ClientQueues。

### 帧级特征货币 `FrameFeature`（在线滑窗 / 离线回放同源）

`FrameFeature`（`app/domain/detection.py`）是特征层输入货币：一帧多流对齐的检测记录 `ts + by_source: Dict[流名, FrameDetections]`，外加**帧级分辨率** `frame_width: Optional[int]` / `frame_height: Optional[int]`。在线写回口物化、离线回放重建，两端同型。

- **分辨率沿每帧轴透传（非检测器输出）**：`frame_shape`（帧分辨率）是 fan-out 前定死的每帧输入常量（一个 `DetectionTask` 扇给 stage 内 N 个模型，各流看同一张图），故拆两个显式字段（避免 `(w,h)` 元组隐式序混淆，本仓库有 frame_shape(H,W,C)/wh(W,H)/frame_width 名义打架前例）。采集链：pool（`detection/pool.py`）从原始帧盖章 `frame_width=frame.shape[1], frame_height=frame.shape[0]`（唯一采集点，原始帧此后即销毁）→ `FrameInference`（`inference/models.py` 同名字段）随传输消息透传 → 写回口（`detection/service.py`）物化进 `FrameFeature` → 落盘/回读随 record 走。缺省 None → 消费方走默认兜底。
- **不再走 `FrameDetections.metadata`**：`FrameDetections` 结构不动，`metadata` 只装真·检测器级数据（`model`/`error`/`mean_brightness`）；检测器（`detection/detector.py`、`workflows/mock.py`）已停产 `frame_shape`。旧的「检测器逐个塞 metadata + 落盘 `_extract_frame_wh` 抠回」机制已删除。
- **在线/离线消费同源**：online `workflows/clean.py._adapt_to_features` 与 offline `offline/segmenters/clean.py` 都从 `FrameFeature.frame_width/height` 读取（缺失该帧留全零行 / 回退默认尺寸）。两条特征管线（online 6 维、offline 113+ 维）仍刻意分离，仅分辨率来源统一。

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

## HLS 文件目录

落盘根目录为 `settings.storage_base_dir`（单一真源，persistence / inference FeatureStore / traceback 三方都读它，不各自派生）。

目录约定：

```text
{storage_base_dir}/{task_id}/{step_id}/
  init.mp4                        # fMP4 init，step 级共享
  raw_segment_{ts_us}.mp4
  processed_segment_{ts_us}.mp4
  raw_playlist.m3u8
  processed_playlist.m3u8
  metadata.json
  .hls_timescale                 # timescale 缓存
  features.jsonl                 # L2 检测特征（inference FeatureStore 落盘，供离线）
  facts.jsonl                    # L3 事实账本（FactLedger，offline 预置）
```

（`keypoints_{ts_us}.json` 死写已删。）追溯、Lab 和媒体访问都按该目录约定定位文件。

## FeatureStore 落盘 record（features.jsonl）

`FeatureStore`（`app/services/inference/feature/store.py`，继承 `_JsonlBuffer`）按 `(task_id, step_id)` 缓冲批量追加写 `features.jsonl`，常开、best-effort（IO 异常只记日志不抛）。`append`/`load` 两端货币均为帧级 `FrameFeature`；磁盘 record 是它的**精简投影**，一对逆运算 `_feature_to_record`/`_record_to_feature`（紧挨放置、互为逆）。

每行一条 record，形状：

```json
{"ts": <float>, "features": {"<流名>": [{"bbox": [x1,y1,x2,y2], "conf": <float>, "cls_id": <int>, "cls": "<class_name>"}, ...], ...}, "frame_width": <int>, "frame_height": <int>}
```

- **`ts`**：帧捕获时间戳，= 该帧 `FrameFeature.ts`，与在线滑窗、HLS 段/keypoints 的 `fd.timestamp` 同源同值，故 feature 行可按 `ts` 精确对上同帧 HLS 证据。反序列化边界统一 `float`（手写 JSONL 可能给 int）。
- **`features`**：`{流名: [检测框...]}`，每框只落 `bbox/conf/cls_id/cls`。**刻意不落**（回读按契约默认还原）：`mask`/`keypoints`（重，seg/pose 才有，离线不消费）、`metadata`/`success`/`error`（离线不消费）。
- **`frame_width`/`frame_height`（帧级分辨率，全命名键）**：仅当两者皆非 None 时落顶层；缺一即整体省略。回读还原到 **`FrameFeature.frame_width/height` 字段**，`FrameDetections.metadata` 保持为空 `{}`。这取代了早前「位置数组 `wh`」与「`_extract_frame_wh` 从检测器 metadata 抠回」两种旧做法——全链路无位置约定。
- **回读**（`load(task_id, step_id) -> List[FrameFeature]`）：单次顺序扫文件，每行 `_record_to_feature` 还原一个 `FrameFeature`（by_source 含该行全部 source，含 detections 为空的 source），按 `ts` 升序；文件缺失返回 `[]`，单行损坏记 warning 跳过。旧 `load(source)`/`load_many` 已删。回读用 `utf-8-sig` 容忍 Windows 手写文件的 UTF-8 BOM。

> **兼容与迁移**：`features.jsonl` 是随 step 目录 TTL 回收的临时件、每 run 重生（`open_fresh` 起始截断分区），故格式演进不做迁移。极端情况（run 跨部署，旧 `wh` 位置数组行被新码读）→ 该行分辨率缺失 → offline 默认兜底（无 crash，仅该 step 用默认尺寸归一化）。旧 `features.jsonl` 因命名键缺省即省略而天然兼容。

同目录还有 `facts.jsonl`：`FactLedger`（同 `_JsonlBuffer` 底座）落 L3 `EventFact`/`SegmentFact`，online 链路不再写，供离线回读（`replace_segments` 支持按 producer 幂等替换分段）。

## Metadata

`metadata.json` 由 HLS 策略更新，包含 task/step、raw/processed 段数量、总时长、首尾时间戳和更新时间。

## 代码来源

- `app/database.py`
- `app/models.py`
- `app/domain/{frame,detection,alarm,render}.py`
- `app/settings.py`（`storage_base_dir` 单一真源）
- `app/services/persistence/strategies/hls_strategy.py`
- `app/services/inference/feature/store.py`（`_feature_to_record`/`_record_to_feature` 对称映射）
- `app/services/inference/models.py`（`FrameInference` 帧级 wh 透传）
- `app/services/inference/detection/{pool,service,detector}.py`（wh 盖章 / 物化 / 停产 frame_shape）
- `app/services/traceback/segment_finder.py`
- `config/persistence_config.yaml`

