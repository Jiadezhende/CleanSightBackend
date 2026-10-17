"""bundle 特征的 numpy 实现，复制自训练仓 ``tools/features_ama/``（经交付包 c7ref）。

只改了 ``clean_bbox_v2._read_object_arrays`` 的输入（框文件路径 → 已解析的逐帧检测行）。
不要改动特征的计算方式：任何数值变化都需要升特征版本并重训。
"""
