"""平台 DB（PostgreSQL，无代码平台托管表），后端只读。

    from app.db.database import get_db
    from app.db.tasks import DBTask       # clean_task
    from app.db.alarms import DBAlarm     # clean_alarm

- `database`：engine / 连接池 / `SessionLocal` / `get_db` / 声明式 `Base`
- `tasks` / `alarms`：一张表一个模块，只放 ORM 映射；运行时契约见 `app.types`，API DTO 跟各自 router 走

零 re-export，调用方一律走深路径。
"""
