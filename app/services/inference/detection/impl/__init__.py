"""检测层业务实现（Detector 子类，一业务点一文件）。

抽象基类在上层 `detection/detector.py`（Detector / YOLODetector）；本目录只放
具体 Detector 子类（流源，无状态，多 Client 共享）。同一业务点的 Operator 在
`temporal/impl/<同名>.py`、离线 Segmenter 在 `offline/impl/<同名>.py`，三者靠
同名文件 + config stage 绑定表达业务聚合。

纯包标记，不做 re-export——StageFactory 经 config 里的全限定 class_path 用 importlib
按需实例化，消费方一律走单文件深路径导入。
"""
