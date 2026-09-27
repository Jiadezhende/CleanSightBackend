"""持久化服务全局单例（唯一定义处）

与 `app/services/inference/online/instance.py` 同一模式：`manager.py` 只管类定义，
要那一个全局实例的人才 import 本模块。

按规范 §6，本单例只许被 `run_control` / `routers/*` / 本包 `lifespan()` 引用。
唯一例外是 `inference/temporal/alarm_sink.py`（告警落库 sink，跨服务但方向正确：
inference 产告警 → persistence 落库），已在门禁白名单中。
"""

from .manager import PersistenceManager

persistence_manager: PersistenceManager = PersistenceManager()
