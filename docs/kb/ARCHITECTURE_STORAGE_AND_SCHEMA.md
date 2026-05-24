> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 存储与 Schema

CleanSight 同时使用数据库表和本地 HLS 文件目录。

## 数据库连接

数据库连接由 `app/database.py` 创建：

- SQLAlchemy `QueuePool`
- `pool_pre_ping=True`
- 常驻连接数 5
- 最大溢出连接 10
- 连接回收 3600 秒

连接字符串来自 `settings.database_url`，由 `CLEANSIGHT_DB_*` 环境变量拼接。

## clean_task

ORM：`DBTask` in `app/models/task.py`

关键字段：

- `_id`：平台主键，varchar。
- `task_id`：业务主键，BigInteger，有索引。
- `source_ip`：当前统一 API 用作 `client_id` 来源。
- `current_step`：当前步骤，字符串。
- `status`、`updated_time`、`start_time`、`end_time`。

运行时 `Task` 是 Pydantic 模型，不直接用于数据库写入。

## clean_alarm

ORM：`DBAlarm` in `app/models/task.py`

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

落盘根目录来自 `config/persistence_config.yaml` 的 `storage.base_dir`，默认 `./database`。

目录约定：

```text
{base_dir}/{task_id}/{step_id}/
  init.mp4
  raw_segment_{ts_us}.mp4
  processed_segment_{ts_us}.mp4
  raw_playlist.m3u8
  processed_playlist.m3u8
  keypoints_{ts_us}.json
  metadata.json
```

追溯、Lab 和媒体访问都按该目录约定定位文件。

## Metadata

`metadata.json` 由 HLS 策略更新，包含 task/step、raw/processed 段数量、总时长、首尾时间戳和更新时间。

## 代码来源

- `app/database.py`
- `app/models/task.py`
- `app/services/persistence/strategies/hls_strategy.py`
- `app/services/traceback/segment_finder.py`
- `config/persistence_config.yaml`

