"""bundle 特征的 numpy 实现，裁剪自训练仓 ``tools/features_ama/``（extractor git_head 642d74f，经交付包 c7ref）。

只保留 bundle 94 列用到的路径；训练仓分 ``full`` / ``fix-compact-pair`` 两遍计算再拼，这里由 ``clean_bbox_v5.build_bundle`` 一遍算完。
文件名与函数名沿用训练仓，便于对照。不要改动特征的计算方式：任何数值变化都需要升特征版本并重训。
"""
