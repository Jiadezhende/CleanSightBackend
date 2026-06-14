---
name: schema-inspect
description: 用于检查 CleanSightBackend PostgreSQL 表结构，对比真实数据库字段和 SQLAlchemy ORM，过滤无代码平台 hidden 字段，并确认 cls_id 等平台字段要求。
---

# 数据库 Schema 检查工作流

## 用途

这个 skill 用于连接真实 PostgreSQL 数据库，读取 CleanSightBackend 业务表结构，
区分业务字段和无代码平台 hidden 字段，并与 SQLAlchemy ORM 模型进行比对。

本文件是 `.claude/skills/schema-inspect/SKILL.md` 的 Codex 中文转换版。

## 触发场景

当用户提到以下任务时，应该先阅读本文件：

- 查看表结构。
- 检查 ORM 和数据库字段是否匹配。
- 确认某个字段是否存在。
- 检查无代码平台 hidden 字段。
- 排查 `_id`、`cls_id`、`clean_task`、`clean_alarm`、`file_path`。
- 手动插入测试数据前确认必填平台字段。

原始 Claude skill 中的 `/schema-inspect` 是触发写法，不是 Codex 里可直接执行的命令。

## 已知业务表

- `clean_task` -> `DBTask`，位于 `app/models/task.py`。
- `clean_alarm` -> `DBAlarm`，位于 `app/models/task.py`。
- `file_path` -> `HLSSegment`，位于 `app/models/frame.py`。

## 平台 hidden 字段

这些字段由无代码平台自动维护，报告时应和业务字段分开：

```python
PLATFORM_HIDDEN_FIELDS = {
    "_id",
    "cls_id",
    "tenant",
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

需要特别注意：

- `_id` 是平台业务表的真实数据库主键。
- `cls_id` 是 NOT NULL 字段，手动插入记录时必须提供。
- `task_id`、`alarm_id` 这类字段是业务 ID，不是数据库主键。

## 读取真实 schema

使用项目的 SQLAlchemy engine：

```python
from app.database import engine
from sqlalchemy import text

table_name = "clean_task"

with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = :table
        ORDER BY ordinal_position
    """), {"table": table_name}).fetchall()
```

## ORM 对比步骤

1. 从 `information_schema.columns` 读取数据库真实字段。
2. 按 `PLATFORM_HIDDEN_FIELDS` 分成业务字段和平台字段。
3. 读取对应 ORM Model 的 `__table__.columns`。
4. 对比：
   - DB 有但 ORM 没有的业务字段。
   - ORM 有但 DB 没有的字段。
   - DB 类型和 ORM 类型不一致的字段。
   - 主键是否符合平台规则。
5. 用简洁表格输出结果。

## 类型映射参考

- `bigint` -> `BigInteger`
- `integer` -> `Integer`
- `text` -> `Text`
- `character varying` -> `String`
- `boolean` -> `Boolean`
- `jsonb` / `json` -> PostgreSQL `JSON`

## cls_id 查询

平台在 `t_ci_class` 表维护业务表的 class ID：

```python
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    row = conn.execute(text("""
        SELECT id, name, display_name
        FROM t_ci_class
        WHERE name = :table_name
    """), {"table_name": "clean_task"}).fetchone()
```

原始 skill 中记录的已知值：

- `clean_task`: `691dd1a8279461135967c843`
- `clean_alarm`: `691e1d83279461135967c890`
- `file_path`: `6921d140279461135967c9bb`

这些值在不同环境中可能变化。用于写入前，应尽量连接目标数据库重新确认。

## 安全注意事项

- 这个 skill 需要真实数据库连接；如果 DB 不可达，只能说明当前环境无法验证。
- schema 检查只应读数据库，不应写入。
- 创建、修改、删除测试数据前必须先向用户确认。
- 最终回答中不要泄露 `.env` 中的密码、token 或完整连接串。

## 建议输出格式

```text
=== clean_alarm (业务字段: N, 平台字段: 11) ===

字段名              DB 类型        ORM 类型       状态
alarm_id            bigint         BigInteger     OK
...

比对结果:
[OK] 所有业务字段已映射
[WARN] DB 字段 xxx 未映射到 ORM
[ERROR] ORM 字段 xxx 在 DB 中不存在
[MISMATCH] 字段 xxx: DB=bigint, ORM=Integer
```
