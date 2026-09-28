"""健康监控 worker 全局单例（唯一定义处）

`worker.py` 只管类定义（测试可自由构造），要那一个全局实例的人才 import 本模块。

构造零副作用——`HealthMonitorWorker.__init__` 不读 yaml、不取协作者单例，
协作者与配置都推迟到 `lifespan()` 里的 `start()` 现取。

按规范 §6，本单例只许被 `run_control` / `routers/*` / 本包 `lifespan()` 引用。
"""

from .worker import HealthMonitorWorker

health_monitor_worker: HealthMonitorWorker = HealthMonitorWorker()
