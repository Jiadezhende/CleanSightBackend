"""兼容入口：旧 HeuristicBrushSegmenter 指向新的 segmenter 模型类。

模型实现已迁移到 app.services.inference.offline.segmenter.brush_rule。
后续新增模型也应放在 offline/segmenter/ 下。
"""

from app.services.inference.offline.segmenter.brush_rule import BrushRuleSegmenter

HeuristicBrushSegmenter = BrushRuleSegmenter

__all__ = ["HeuristicBrushSegmenter", "BrushRuleSegmenter"]
