"""随权重固定的契约：类序、特征版本 / 维度、物料文件名、checkpoint 格式。改动必须换权重。"""

# 模型输出列序 → 动作名；id 0 是背景类，解码时跳过
LABELS = (
    "idle",
    "long_brush_insert",
    "long_brush_withdraw",
    "short_brush_cleaning",
    "flush",
    "air_injection",
)

FEATURE_VERSION = "clean_bbox_v2_top1_impute"
FEATURE_DIM = 113

# 物料：model_dir/MODEL_FILE（原 app/data/clean-offline-mstcn.pt）。checkpoint 是带元数据的训练导出：
# {state_dict, class_names, feature_names, feature_version, normalizer_mean / normalizer_std（ndarray）, …}。
# 含 numpy 数组，须 weights_only=False（存量物料例外）；class_names / feature_names / feature_version 参与加载校验。
MODEL_FILE = "model.pt"
