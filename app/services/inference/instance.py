"""InferenceManager 单例。

与 `client_manager` / `stream_service` / `persistence_manager` 对齐：每个服务在自己
包内持有单例。放独立 leaf 模块（而非 `manager.py` 尾部或包 `__init__`）是刻意为之——
`InferenceManager()` 构造较重（加载 stage 配置、建 worker 池），只应在消费方显式
import 本模块时才构造，避免任何 `import app.services.inference.*` 触发 eager 构造。

无需注入 fps/段长：CQ 段长真源在 ClientConfig.ca_segment_len（ca_segment_seconds×raw_fps），
经 cq_kwargs 注入；可视化轮询率由 manager 内部直读 settings.inference_fps 派生。
"""

from app.services.inference.manager import InferenceManager

inference_manager = InferenceManager()
