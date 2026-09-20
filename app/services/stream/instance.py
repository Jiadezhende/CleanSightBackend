"""流服务全局单例（唯一定义处）

与 `app/services/inference/instance.py` 同一模式：`manager.py` 只管类定义（测试可自由
构造，见 `tests/test_reconnect_on_initial_failure.py`），要那一个全局实例的人才 import 本模块。

按规范 §6，本单例只许被 `run_control` / `routers/*` / 本包 `lifespan()` 引用。
"""

from app.services.stream.manager import StreamService

stream_service: StreamService = StreamService()
