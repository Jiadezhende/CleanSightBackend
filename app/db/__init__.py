"""平台 DB（PostgreSQL，无代码平台托管表），后端只读。

    from app.db import tasks as db_tasks      # clean_task：DBTask + query_*
    from app.db import alarms as db_alarms    # clean_alarm：DBAlarm + query_* / detected_at_ms

- `database`：engine / 连接池 / `SessionLocal` / `get_db` / 声明式 `Base`
- `tasks` / `alarms`：一张表一个模块，ORM 映射 + 该表的 `query_*`（自开自关 session，失败抛
  `DatabaseError`）；运行时契约见 `app.types`，API DTO 跟各自 router 走

**边界**：只许 import `app.db` / `app.types` / `app.settings`——不依赖 services / storage / routers，
由 `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES` 门禁执行。零 re-export，调用方一律走深路径。
"""
