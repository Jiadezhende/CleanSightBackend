"""CLEAN 历史离线策略：因果滑窗 GRU（特征 ama-v3-concat23-nodep-226d = v2 ⊕ v3，7.5fps）。

策略类 ``segmenter.CleanGRUNodepSegmenter``（原 ``impl/clean.py`` 的 ``CleanNodepGRUSegmenter``，按「架构在前、特征在后」改名）。
代码原样迁自旧 clean.py，只改了检测归拢的数据结构（每条检测一个 [T,5] 稠密数组 → 按帧分桶；数值逐位一致）。
特征的计算方式不许改：任何数值变化都要换特征版本并重训。
"""
