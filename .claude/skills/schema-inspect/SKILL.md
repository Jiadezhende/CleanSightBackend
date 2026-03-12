---
name: schema-inspect
description: "Inspect database schema for CleanSightBackend tables. Connects to the actual PostgreSQL database, reads column metadata, separates business fields from no-code platform hidden fields, and cross-references with existing SQLAlchemy ORM models to find mismatches. Use when: 查看表结构、schema对比、字段校验、检查ORM匹配、inspect schema、check table columns、数据库字段."
---

# Schema Inspect — 无代码平台数据库 Schema 校验工具

## 用途

连接实际 PostgreSQL 数据库，读取表的真实 schema，过滤掉无代码平台的 hidden 字段，并与代码中的 SQLAlchemy ORM Model 做交叉比对。

## 使用方式

用户调用 `/schema-inspect` 时，可指定表名（如 `/schema-inspect clean_alarm`），也可不指定参数来检查所有已知业务表。

## 执行步骤

### Step 1: 连接数据库并读取实际 schema

使用项目的 SQLAlchemy engine 查询 `information_schema.columns`：

```python
from app.database import engine
from sqlalchemy import text

TABLE_NAME = "<用户指定的表名，或遍历已知表>"

with engine.connect() as conn:
    result = conn.execute(text("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = :table
        ORDER BY ordinal_position
    """), {"table": TABLE_NAME})
    columns = result.fetchall()
```

### Step 2: 分离业务字段与平台 Hidden 字段

以下字段是无代码平台自动添加的维护字段，每张表都有，业务代码应忽略：

```python
PLATFORM_HIDDEN_FIELDS = {
    "_id",        # 平台内部主键 (varchar)，非业务主键
    "cls_id",     # 平台 class/model 标识
    "tenant",     # 租户标识
    "create_user",
    "create_time",
    "last_update_user",
    "last_update_time",
    "ci_source",
    "ci_status",
    "hash_sign",
    "t_version",
}
```

将查询结果分为两组输出：
- **业务字段**：不在 `PLATFORM_HIDDEN_FIELDS` 中的列
- **平台字段**：在 `PLATFORM_HIDDEN_FIELDS` 中的列（折叠显示即可）

### Step 3: 查找对应的 ORM Model

已知的表与 ORM Model 映射关系：

| 表名 | ORM Model | 文件位置 |
|------|-----------|----------|
| `clean_task` | `DBTask` | `app/models/task.py` |
| `clean_alarm` | `DBAlarm` | `app/models/task.py` |
| `file_path` | `HLSSegment` | `app/models/frame.py` |

读取 ORM Model 的 `__table__.columns`，提取已映射的列名和类型。

### Step 4: 交叉比对并输出报告

对比 DB 实际字段与 ORM 映射字段，报告：

1. **DB 有但 ORM 没有的业务字段**（可能需要补充映射）
2. **ORM 有但 DB 没有的字段**（代码中的幽灵字段，会导致运行时错误）
3. **类型不匹配**（如 DB 是 `bigint` 但 ORM 用 `Integer`，DB 是 `text` 但 ORM 用 `String`）
4. **主键正确性**（平台表的 PK 必须是 `_id` varchar，不能是业务字段）

### 输出格式

```
=== clean_alarm (业务字段: N 个, 平台字段: 11 个) ===

业务字段:
  字段名              DB类型          ORM类型         状态
  alarm_id            bigint          BigInteger      OK
  task_id             bigint          BigInteger      OK
  step_id             bigint          BigInteger      OK
  ...

比对结果:
  [OK] 所有业务字段已映射且类型匹配
  [WARN] DB 字段 xxx 未在 ORM 中映射
  [ERROR] ORM 字段 xxx 在 DB 中不存在
  [MISMATCH] 字段 xxx: DB=bigint, ORM=Integer
```

## 已知业务表清单

如果用户不指定表名，默认检查以下表：
- `clean_task` — 清洗任务表
- `clean_alarm` — 告警表
- `file_path` — HLS 视频段路径表

## 类型映射参考

| PostgreSQL 类型 | 正确的 SQLAlchemy 类型 |
|-----------------|----------------------|
| `bigint` | `BigInteger` |
| `integer` | `Integer` |
| `text` | `Text` |
| `character varying` | `String` |
| `boolean` | `Boolean` |
| `jsonb` / `json` | `JSON` (from `sqlalchemy.dialects.postgresql`) |

## cls_id 探查方案

`cls_id` 是平台的 class 标识字段，**每张表都有且 NOT NULL**。当需要手动插入记录（如创建测试数据）时，必须填写正确的 `cls_id`。

### 如何查找某张表的 cls_id

平台在 `t_ci_class` 表中维护了所有业务表的元数据。通过 `name` 字段匹配表名：

```python
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    result = conn.execute(text("""
        SELECT id, name, display_name FROM t_ci_class
        WHERE name = :table_name
    """), {"table_name": "clean_task"})
    row = result.fetchone()
    # row.id 就是该表的 cls_id
```

### 已知表的 cls_id 值

| 表名 | cls_id | 备注 |
|------|--------|------|
| `clean_task` | `691dd1a8279461135967c843` | 清洗任务表 |
| `clean_alarm` | `691e1d83279461135967c890` | 告警表 |
| `file_path` | `6921d140279461135967c9bb` | HLS 视频段路径表 |

### 手动插入记录时的必填平台字段

虽然大部分平台 hidden 字段是 nullable 的，但以下两个是 **NOT NULL**，手动插入时必须提供：

| 字段 | 类型 | 说明 | 生成方式 |
|------|------|------|----------|
| `_id` | varchar | 平台主键 | `uuid.uuid4().hex` |
| `cls_id` | varchar | class 标识 | 从上表查找，或查询 `t_ci_class` |

### t_ci_class 表结构参考

该表的主要列：`id`, `name`, `display_name`, `remark`, `parent_cls_id`, `folder_id`, `attributes`, `status` 等。
注意：该表的主键列名是 `id`（不是 `_id`），与业务表不同。

## 注意事项

- `_id` (varchar) 是每张表的真实主键，由平台生成，ORM 中必须声明为 `Column(String, primary_key=True)`
- `cls_id` (varchar) 是 NOT NULL 的平台字段，手动写入数据时必须填写（见上方探查方案）
- 业务主键（如 `task_id`, `alarm_id`）不是数据库层面的 PK，只是业务唯一标识
- 平台 hidden 字段无需在 ORM 中声明，SQLAlchemy 会自动忽略未映射的列（但 `cls_id` 因为 NOT NULL，如果 ORM 需要插入数据则必须映射）
- `create_time` 虽然是平台字段，但如果业务需要用于排序（如告警按创建时间排序），可以额外映射
