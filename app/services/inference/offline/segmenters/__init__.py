"""离线分割策略实现集 —— 一策略一模块，自包含（输入预处理 + 模型 + 解码内聚一处）。

纯包标记，不做 re-export——StageFactory 经 YAML `offline.class` 全限定路径用 importlib 按需
实例化（见 stage_factory.create_offline_segmenter）；新增策略只往本目录加一个自包含模块 +
YAML `offline` 段切 `class`，实现不散落到框架层（base/runner/cli）。
"""

from app.services.inference.offline.segmenters.brush_rule import BrushRuleSegmenter

__all__: list[str] = ["BrushRuleSegmenter"]
