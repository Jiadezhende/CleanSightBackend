"""时序层业务实现（Operator 子类，一业务点一文件）。

抽象基类在上层 `temporal/operator.py`（Operator / TemporalOperator）；本目录只放
具体 Operator 子类（流算子，有状态，analyze 推进 _sm + judge 出告警，每 Client 独立）。
同一业务点的 Detector 在 `detection/impl/<同名>.py`、离线 Segmenter 在
`offline/impl/<同名>.py`，三者靠同名文件 + config stage 绑定表达业务聚合。

纯包标记，不做 re-export——StageFactory 经 config 里的全限定 class_path 用 importlib
按需实例化，消费方一律走单文件深路径导入。
"""
