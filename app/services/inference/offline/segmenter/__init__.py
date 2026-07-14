"""离线时序分割模型集合。

本包只放运行时推理需要的模型和特征转换逻辑。
训练数据转换、数据集划分、训练循环等实验代码不放入业务后端分支。
"""

from app.services.inference.offline.segmenter.brush_rule import BrushRuleSegmenter
from app.services.inference.offline.segmenter.features import FeatureVectorizer, ModelInput

SEGMENTER_REGISTRY = {
    "brush_rule": BrushRuleSegmenter,
}


def create_segmenter(name: str):
    """按名称创建离线分割模型。"""
    try:
        return SEGMENTER_REGISTRY[name]()
    except KeyError as exc:
        available = ", ".join(sorted(SEGMENTER_REGISTRY))
        raise ValueError(f"未知离线模型: {name!r}，可选: {available}") from exc


__all__ = [
    "BrushRuleSegmenter",
    "FeatureVectorizer",
    "ModelInput",
    "SEGMENTER_REGISTRY",
    "create_segmenter",
]
