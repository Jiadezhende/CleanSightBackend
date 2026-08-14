"""离线特征导出器 —— 把 (task_id, step_id) 的落盘特征转成可直接喂模型的输入样例。

offline 段的**第二条管线**（第一条是 runner→segment→FactLedger）：那条产事实，这条产模型
输入。两条同吃稳定存储键 `(task_id, step_id)`、同不接 client/CQ/DB，但产物与消费方不同，
故各自独立编排、独立入口，不复用彼此的 Runner。

本包只放**框架管件**（编排 / 货币壳 / CLI），零业务知识：具体 recipe 是业务实现，住在
`offline/impl/<业务>.py`，由 `ExportSpec.recipe` 的全限定路径经 importlib 取用——与
StageFactory 取 `offline.class` 同款。这样同一份 recipe 既产训练样例、又做将来 Segmenter
的线上特征转换，单一真源是结构性保证。

手动跑：
    python -m app.services.inference.offline.export.cli \\
        --task-id 100 --step-id 2 \\
        --recipe app.services.inference.offline.impl.clean.export_r0
"""

from app.services.inference.offline.export.models import (
    ExportQuality,
    ExportResult,
    ExportSpec,
    VisualFrames,
)
from app.services.inference.offline.export.runner import ExportRunner

__all__: list[str] = [
    "ExportRunner",
    "ExportSpec",
    "ExportResult",
    "ExportQuality",
    "VisualFrames",
]
