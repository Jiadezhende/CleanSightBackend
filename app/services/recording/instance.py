"""录制服务全局单例（唯一定义处）。

与 `app/services/persistence/instance.py` 同一模式：`service.py` 只管类定义（测试自行
构造并注入假的 `clients`），要那一个全局实例的人才 import 本模块。

按规范 §6，本单例只许被 `run_control` / `routers/*` / 本包 `lifespan()` 引用，外加一处
具名例外：`health_monitor/manager.py` 断流时要调 `request_residual_flush`（门禁
`test_singleton_reference_surface`）。包内的 `_sweeper` **不** import 它——服务把自己
注入给 sweeper，方向向下。
"""

from app.services.recording.service import RecordingService

recording_service: RecordingService = RecordingService()
