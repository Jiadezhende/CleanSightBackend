"""离线分割策略实现集 —— 一策略一自包含单文件（预处理 + 模型 + 解码内聚一处）。

StageFactory 经 YAML `offline.class` 全限定路径用 importlib 按需实例化（见
stage_factory.create_offline_segmenter）；新增策略 = 往本目录加一个自包含单文件
`<stage>.py` + YAML `offline` 段切 `class`，实现不散落到框架层（segmenter/runner/cli）。
"""

from app.services.inference.offline.segmenters.clean import CleanSegmenter
from app.services.inference.offline.segmenters.mock import MockSegmenter

__all__: list[str] = ["CleanSegmenter", "MockSegmenter"]
