> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 存储与 Schema

CleanSight 同时使用数据库表和本地 HLS 文件目录。

## 数据模型分层（domain / ORM / DTO）

运行时数据按来源/生命周期分层，依赖方向单一（domain 是叶子，无反向依赖）：

- `app/domain/`：跨服务共享契约，纯 dataclass，**零服务依赖**。按 concern 分文件：`frame.py`(`Frame`) / `detection.py`(`Detection`,`FrameDetections`) / `alarm.py`(`AlarmType`,`AlarmMetric`,`Alarm`) / `render.py`(`RenderSpec`,`RenderItem`,`RenderType`)。调用方从子模块显式 import。
- `app/models.py`：仅 ORM 行对象 `DBTask`/`DBAlarm`。
- DTO：HTTP 请求/响应模型就地定义在各 router，不放 domain。
- inference 内部件（`FrameInference`/`DetectionTask`/`EventFact`/`SegmentFact`）留在 `inference/models.py`，不入 domain（pipeline-internal）。

关键归位：`Alarm` 吸收旧 `AlarmRecord`（单一告警抽象，`mode/stage/seq` 由 `alarm_sink` 在落库边界补齐）；`AlarmMetric` 由 Judge/Operator 显式设定，非下游反推；旧运行时 `CleaningTask` VO（曾 Pydantic）已删，身份 primitives（task_id/step_id）直挂 ClientQueues。

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
```

（`keypoints_{ts_us}.json` 死写已删。）追溯、Lab 和媒体访问都按该目录约定定位文件。

## Metadata

`metadata.json` 由 HLS 策略更新，包含 task/step、raw/processed 段数量、总时长、首尾时间戳和更新时间。

## 代码来源

- `app/database.py`
- `app/models.py`
- `app/domain/{frame,detection,alarm,render}.py`
- `app/settings.py`（`storage_base_dir` 单一真源）
- `app/services/persistence/strategies/hls_strategy.py`
- `app/services/inference/feature/store.py`
- `app/services/traceback/segment_finder.py`
- `config/persistence_config.yaml`

