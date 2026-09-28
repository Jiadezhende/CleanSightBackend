# 平台 DB 独立成 app/db/：database 搬家，models 按表拆成 tasks / alarms

> **变更状态**：已完成（2026-09-28）——纯搬迁与拆文件，不改运行逻辑、不迁移任何查询
> **知识库**：待沉淀

## 概述

`app/database.py` → `app/db/database.py`；`app/models.py` 按表拆成 `app/db/tasks.py`（`DBTask`）与
`app/db/alarms.py`（`DBAlarm`），`app/models.py` 删除；新建标记型 `app/db/__init__.py`。routers、integration_tests、
schema-inspect skill、各处边界声明与 README 同步改路径。

## 变更背景

- **现状**：平台 DB 的连接池与 ORM 以两个文件平铺在 `app/` 根，与 `main.py` / `settings.py` 等组装入口混在一层；两张表挤在一个 `models.py`。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波：DB 独立成 `db/` 层（平台 DB，只读）。本篇只搬表映射；routers 里散落的 `clean_task` / `clean_alarm` 查询搬进 `db/` 是后续逻辑抽取批次的事。

## 方案详情

### 全景：一处搬家 + 一处按表拆分

```text
app/database.py  → app/db/database.py      engine / SessionLocal / get_db / Base，内容不变
app/models.py    → app/db/tasks.py         DBTask（clean_task）
                 → app/db/alarms.py        DBAlarm（clean_alarm）
                   app/db/__init__.py      新建：纯 docstring、零 re-export
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 包内文件 | `app/db/` | §1 |
| 引用改写 | `app/routers/{api,lab,task,traceback}.py`、`integration_tests/{test_multi_client,utils}.py`、`.claude/skills/schema-inspect/SKILL.md` | §2 |
| 边界声明与门禁注释 | `app/services/utils/__init__.py`、`app/storage/__init__.py`、`tests/test_import_hygiene.py`、`README.md` | §3 |

### 1. `app/db/` 包内

- `database.py`：`from .settings import settings` → `from app.settings import settings`（跨包改绝对，§8）；logger 名 `"app.database"` → `__name__`（即 `"app.db.database"`，`config/logging.json` 未按名配置它，只影响日志行里的 logger 名）。其余不变。
- `tasks.py` / `alarms.py`：各自 `from .database import Base`，两张表仍共用同一个 `Base.metadata`；列定义逐字搬运。
- `__init__.py`：写明平台 DB、后端只读（代码里无 `add` / `commit` / `delete`），零 re-export，调用方走深路径 `from app.db.tasks import DBTask`。

### 2. 引用改写

`from app.database import get_db` → `from app.db.database import get_db`；
`from app.models import DBTask` / `DBAlarm` → `from app.db.tasks import DBTask` / `from app.db.alarms import DBAlarm`。
测试里 `patch("app.routers.api.get_db", ...)` 按 router 模块内名字打桩，不受影响。

schema-inspect skill：`engine` 导入路径改为 `app.db.database`；表 → ORM 映射表的文件位置改为 `app/db/tasks.py` / `app/db/alarms.py`，删去早已不存在的 `file_path` → `HLSSegment`（`app/models/frame.py`）一行（`file_path` 表本身在 cls_id 清单里的条目保留）。

### 3. 边界声明与门禁

- `app/services/utils/__init__.py`、`app/storage/__init__.py` 的「不许 import `app.database` / `app.models`」改为 `app.db`。
- `tests/test_import_hygiene.py`：`LAYER_PACKAGES` 上方注释与 `test_layer_package_imports_only_whitelisted_app_modules` docstring 同步改名。白名单本身不含 `app.db`，storage / services.utils 对它的禁令照旧生效；`BUDGET` / `SINGLETONS` 不涉及。
- `README.md` 项目结构：删 `database.py` / `models.py` 两行，`storage/` 后加 `db/`。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 843 passed, 8 skipped |
| `import app.main` + `app.db.*` 冒烟、`integration_tests` 两文件 `py_compile` | 通过；`DBTask` / `DBAlarm` 表名仍为 `clean_task` / `clean_alarm` |
| 残留检查 `git grep` `app.database`、`app.models`、`app/database`、`app/models` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| routers 里的 `clean_task` / `clean_alarm` 查询仍散在 `api` / `task` / `lab` / `traceback` | `db/` 暂时只有表映射，没有 `query_*` | 后续逻辑抽取批次搬进 `db/tasks.py` / `db/alarms.py` |
| `app/db` 尚未登记为 `LAYER_PACKAGES` 分层包 | 「db 只依赖 settings」目前无门禁 | 查询搬进来后再加白名单与逐模块 `BUDGET` |
| `docs/kb/`（`ARCHITECTURE_STORAGE_AND_SCHEMA.md` 等）仍写 `app/database.py` / `app/models.py` | 读 KB 时路径过时 | KB 融合时按本篇改名 |
