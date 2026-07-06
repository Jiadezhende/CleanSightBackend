"""InferenceManager 单例。

与 `client_manager` / `stream_service` / `persistence_manager` 对齐：每个服务在自己
包内持有单例。放独立 leaf 模块（而非 `manager.py` 尾部或包 `__init__`）是刻意为之——
`InferenceManager()` 构造较重（加载 stage 配置、建 worker 池），只应在消费方显式
import 本模块时才构造，避免任何 `import app.services.inference.*` 触发 eager 构造。

参数走 settings 单一真源（见 app/settings.py）。
"""

from app.services.inference.manager import InferenceManager
from app.settings import settings

inference_manager = InferenceManager(
    rt_fps=settings.raw_fps,
    ca_segment_seconds=int(settings.ca_segment_len / settings.raw_fps),  # 帧数转秒
)
