"""bundle 特征的 numpy 实现，裁剪自训练仓 ``tools/features_ama/``（extractor git_head 642d74f，经交付包 c7ref）。

只保留 bundle 94 列用到的路径（v4 只剩 ``V4_PAIRS``，已并入 v5 的 ``COMPACT_PAIRS``）；文件名与函数名沿用训练仓，便于对照。
输入改为已解析的逐帧检测行（``clean_bbox_v2._read_object_arrays``）。不要改动特征的计算方式：任何数值变化都需要升特征版本并重训。
"""
