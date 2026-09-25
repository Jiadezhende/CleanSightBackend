"""离线作业服务全局单例（唯一定义处）。

`service.py` 只管类定义（测试自行构造并注入假的 `clients` / `launcher`），要那一个全局实例的人
才 import 本模块。按规范 §6，只许被 `run_control` / `routers/*` / `inference` 包的 `lifespan()` 引用。
"""

from .service import OfflineJobService

offline_job_service: OfflineJobService = OfflineJobService()
