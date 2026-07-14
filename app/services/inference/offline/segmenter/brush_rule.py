"""刷洗动作离线分割模型：规则 baseline。

这是一期可用模型类，目的是跑通后端离线推理链路：
    OfflineFeatureSequence -> FeatureVectorizer -> ModelInput -> FramePrediction[]

模型输入：
    FeatureVectorizer 输出的 62 维时序特征，形状为 [time, feature_dim]。

模型输出：
    每帧一个 FramePrediction，worker 会继续合并成行为时间线。

注意：
    该模型是运行时 baseline，不包含训练逻辑。后续接入 MS-TCN/ASFormer 时，可以实现同样的
    OfflineTemporalModel.predict 接口，并复用 FeatureVectorizer 的输入数据。
"""

from __future__ import annotations

from app.services.inference.offline.interfaces import FramePrediction, OfflineFeatureSequence
from app.services.inference.offline.segmenter.features import FeatureVectorizer, ModelInput


class BrushRuleSegmenter:
    """基于 62 维特征的刷洗动作分割 baseline。

    判断范围先覆盖当前清洗阶段最需要的动作：
        short_brush_cleaning / long_brush_insert / flush / air_injection / idle。

    long_brush_withdraw 需要更明确的长刷轨迹或时序方向信息；当前检测框输入不足时暂不强行输出，
    避免把插入和拔出混淆。
    """

    name = "brush_rule_segmenter"
    version = "brush_rule_v1"
    labels = (
        "idle",
        "long_brush_insert",
        "long_brush_withdraw",
        "short_brush_cleaning",
        "flush",
        "air_injection",
    )

    def __init__(self, vectorizer: FeatureVectorizer | None = None):
        self.vectorizer = vectorizer or FeatureVectorizer()

    def predict(self, sequence: OfflineFeatureSequence) -> list[FramePrediction]:
        """把检测序列转成模型输入，并返回逐帧预测。"""
        model_input = self.vectorizer.transform(sequence)
        name_to_idx = {name: idx for idx, name in enumerate(model_input.feature_names)}
        predictions: list[FramePrediction] = []

        for row, timestamp in zip(model_input.features, model_input.timestamps):
            label, confidence, scores = self._predict_row(row, name_to_idx)
            predictions.append(
                FramePrediction(
                    timestamp=timestamp,
                    label=label,
                    confidence=round(confidence, 5),
                    scores={key: round(value, 5) for key, value in scores.items()},
                )
            )
        return predictions

    def build_model_input(self, sequence: OfflineFeatureSequence) -> ModelInput:
        """暴露特征转换结果，便于端到端测试和后续真实模型接入调试。"""
        return self.vectorizer.transform(sequence)

    @staticmethod
    def _value(row: list[float], name_to_idx: dict[str, int], name: str) -> float:
        """安全读取特征列。"""
        idx = name_to_idx.get(name)
        if idx is None or idx >= len(row):
            return 0.0
        return float(row[idx])

    def _presence(self, row: list[float], name_to_idx: dict[str, int], obj: str) -> float:
        """读取目标存在强度。count 特征存的是 count/3，这里转回 0-1 存在分数。"""
        return min(1.0, self._value(row, name_to_idx, f"{obj}_count") * 3.0)

    def _predict_row(self, row: list[float], name_to_idx: dict[str, int]) -> tuple[str, float, dict[str, float]]:
        """对一帧模型输入做动作判断。"""
        hand = self._presence(row, name_to_idx, "hand")
        short_brush = self._presence(row, name_to_idx, "short_brush")
        long_brush = max(
            self._presence(row, name_to_idx, "long_brush"),
            self._presence(row, name_to_idx, "brush_tip_out"),
        )
        syringe = self._presence(row, name_to_idx, "syringe")
        air_gun = self._presence(row, name_to_idx, "air_gun")
        scope_control = self._presence(row, name_to_idx, "scope_control_body")
        scope_mid = self._presence(row, name_to_idx, "scope_mid_section")
        scope_distal = self._presence(row, name_to_idx, "scope_distal_end")

        hand_short_valid = self._value(row, name_to_idx, "hand_to_short_brush_valid")
        short_scope_valid = self._value(row, name_to_idx, "short_brush_to_scope_control_body_valid")
        long_scope_valid = self._value(row, name_to_idx, "long_brush_to_scope_mid_section_valid")

        short_context = max(hand, scope_control, scope_distal, hand_short_valid, short_scope_valid)
        long_context = max(hand, scope_mid, scope_distal, long_scope_valid)

        scores = {
            "idle": 0.05,
            "long_brush_insert": min(long_brush, long_context) if long_brush else 0.0,
            "long_brush_withdraw": 0.0,
            "short_brush_cleaning": min(short_brush, short_context) if short_brush else 0.0,
            "flush": syringe,
            "air_injection": air_gun,
        }

        # 明确器械动作优先，其次短刷，再其次长刷；同一帧多类框共现时结果更稳定。
        for label in ("air_injection", "flush", "short_brush_cleaning", "long_brush_insert"):
            if scores[label] > 0.0:
                return label, max(0.2, min(1.0, scores[label])), scores
        return "idle", 1.0, scores
