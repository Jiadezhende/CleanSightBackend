"""CLEAN 历史离线策略：MS-TCN + BiLSTM（特征 clean_bbox_v2_top1_impute，113 维）。

策略类 ``segmenter.CleanMSTCNBiLSTMSegmenter``。代码原样迁自旧 ``impl/clean.py``（同名类 + ``_CleanTorchSegmenter`` 基类摊平），
只改了检测归拢的数据结构（每条检测一个 [T,5] 稠密数组 → 按帧分桶；候选顺序与打分不变，数值逐位一致）。
特征的计算方式不许改：任何数值变化都要换特征版本并重训。
"""
