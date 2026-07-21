"""推理检测任务实现（可插拔，保持内聚：Detector + Operator 同文件）。

抽象基类已上移分层包：
    流源 Detector / YOLODetector → app.services.inference.detection.detector
    流算子 Operator → app.services.inference.temporal.operator

本目录只放具体检测任务（一任务一文件）：
    Detector（流源，无状态）：BubbleDetector, BendingDetector, MockDetector,
        CleanLargeDetector, CleanSmallDetector
    Operator（流算子，有状态，analyze 推进状态 + judge 出告警）：
        BubbleOperator, BendingOperator, MockOperator

纯包标记，不做 re-export——StageFactory 经 config 里的全限定 class_path 用 importlib
按需实例化（见 stage_factory._import_class），消费方一律走单文件深路径导入
（`from app.services.inference.workflows.bubble import BubbleDetector, ...`），
避免 import 本包即 eager 拉起全部任务模块。
"""
