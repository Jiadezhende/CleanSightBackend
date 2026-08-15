"""
推理服务模块

提供统一的推理、时序分析、可视化和持久化接口，以及具体检测任务实现。

分层（按处理流程）：契约包 = 顶层基类/框架 + `impl/` 子层放业务实现，三包对称。
    detection/     目标检测层 (L1)：Detector 抽象 + dispatcher/pool/service；impl/ 放 Detector 子类
    feature/       feature_store 层 (L2)：FeatureStore（在线写）/ FactLedger（离线预留，休眠）
    temporal/      时序分析层 (L3/L4)：Operator 抽象 + actor；impl/ 放 Operator 子类
    visualization/ 可视化层：worker/pool/visualizer
    offline/       离线段：OfflineSegmenter 抽象 + runner/cli；impl/ 放 Segmenter 子类
顶层平铺跨层基础设施：manager / config / naming / stage_factory / models。

一个检测点（业务）的三段实现放各契约包 impl/ 下的**同名文件**：
`detection/impl/<x>.py`(Detector) + `temporal/impl/<x>.py`(Operator) + 可选 `offline/impl/<x>.py`(Segmenter)，
一文件一基类；业务聚合由 config stage 绑定表达（见 config/inference_config.yaml）。

本 `__init__` 刻意**不做任何 re-export**（纯包标记）：与 [instance.py] 的
"避免任何 `import app.services.inference.*` 触发 eager 构造" 同一原则——顶层平铺
re-export 会让即便只取轻量 `.models.FrameInference` 的调用方也拉起 YOLO/cv2/impl
的重导入链。消费方一律走显式深路径按需导入：

    单例          from app.services.inference.instance import inference_manager
    总编排        from app.services.inference.manager import InferenceManager
    检测基类      from app.services.inference.detection.detector import Detector, YOLODetector
    时序基类      from app.services.inference.temporal.operator import Operator
    feature_store from app.services.inference.feature.store import FeatureStore, FactLedger
    具体任务      from app.services.inference.detection.impl.bubble import BubbleDetector
                  from app.services.inference.temporal.impl.bubble import BubbleOperator
    工厂/配置     from app.services.inference.stage_factory / .config
    数据模型      from app.services.inference.models import FrameInference

内部管件（dispatcher / pool / service / actor / visualization worker）不再对外暴露，
按需从各自深路径导入。
"""
