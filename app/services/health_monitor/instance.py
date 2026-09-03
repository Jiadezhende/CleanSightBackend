"""健康监控全局单例（唯一定义处）

与 `app/services/inference/instance.py` 同一模式：把「类」（`manager.py`，测试可自由
构造）和「那一个全局实例」分成两个模块，只有明确要单例的人才为它付构造代价。

这里的构造是零副作用的——`GlobalHealthMonitor.__init__` 不读 yaml、不取协作者单例，
三个协作者与配置都推迟到 `lifespan()` 里的 `start()` 现取。

按规范 §6，本单例只许被 `run_control` / `routers/*` / 本包 `lifespan()` 引用。
"""

from app.services.health_monitor.manager import GlobalHealthMonitor

health_monitor: GlobalHealthMonitor = GlobalHealthMonitor()
