"""CLEAN 历史离线策略：mstcn2-bundle（特征 ama-v5-bundle-94d → MS-TCN2，15fps，6 类）。

策略类 ``segmenter.CleanMSTCN2BundleSegmenter``。原只在未合入分支 ``feature/offline-v5-bundle-mstcn2``（3454d94）上，
那版读框与选槽位随帧数平方增长；这里改用 mstcn2-role-embed 交付包里已提速的 ``features_ama`` + ``bundle`` 实现
（按帧分桶，与训练侧特征逐位一致），网络与 pp20 后处理同 role-embed 包的拷贝。特征的计算方式不许改。
"""
