"""随权重固定的契约：类序、特征版本 / 维度、入模帧率、窗口与网络结构、物料文件名、checkpoint 格式。改动必须换权重。"""

LABELS = (
    "idle",
    "water_injection",
    "flush",
    "long_brush_insert",
    "long_brush_withdraw",
    "short_brush_cleaning",
)

FEATURE_VERSION = "ama-v3-concat23-nodep-226d"
FEATURE_DIM = 226
DEPRECATED_OBJECTS = ("scope_distal_end", "short_brush", "long_brush")  # 读入层丢弃，对应块全零

WINDOW = 16                       # 因果窗口帧数（每帧取以它为末帧的窗口）
STRUCTURE = dict(hidden=128, num_layers=3)

# 物料：model_dir/MODEL_FILE（原 app/data/clean-offline-gru-nodep.pt）。checkpoint 是训练状态：
# {schema_version, checkpoint_kind, epoch, model_state, optimizer_state, …}，weights_only=True，只取 model_state（strict）。
MODEL_FILE = "model.pt"
