"""
推理服务模块

提供统一的推理、时序分析、可视化和持久化接口，以及具体检测任务实现。

子包分三类角色（规范 §1）——**同一目录深度不代表同一种东西**，加子包前先认领是哪类：

1. **契约包**：一个基类 + 它的框架管件 + `impl/` 放业务实现，三包对称，加新检测点只碰 `impl/`。
    detection/     目标检测 (L1)：Detector 抽象 + dispatcher/pool/service；impl/ 放 Detector 子类
    temporal/      时序分析 (L3/L4)：Operator 抽象 + actor；impl/ 放 Operator 子类
    offline/       离线段：OfflineSegmenter 抽象 + runner/cli；impl/ 放 Segmenter 子类
2. **基础设施包**：无基类、无 `impl/`，只提供一种被上面几层共用的能力，不随检测点增长。
    feature/       FeatureStore（在线写）/ FactLedger（离线预留，休眠）
3. **活体包**：由 manager 持有、有独立起停的 worker 池，生命周期跟着 `manager.start()/stop()`。
    visualization/ worker/pool/visualizer

顶层平铺跨层基础设施：manager / config / naming / stage_factory / types。
`offline/cli.py` 是 `python -m` 离线入口，**不得被包内任何其他模块 import**（现状零反向引用）。

一个检测点（业务）的三段实现放各契约包 impl/ 下的**同名文件**：
`detection/impl/<x>.py`(Detector) + `temporal/impl/<x>.py`(Operator) + 可选 `offline/impl/<x>.py`(Segmenter)，
一文件一基类；业务聚合由 config stage 绑定表达（见 config/inference_config.yaml）。

本 `__init__` 刻意**不做任何 re-export**（只留 docstring + 下面的 `lifespan()`）：与 [instance.py] 的
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
    数据模型      from app.services.inference.types import FrameInference

内部管件（dispatcher / pool / service / actor / visualization worker）不再对外暴露，
按需从各自深路径导入。

唯一例外是下面的 `lifespan()`：生命周期归服务包自己（规范 §3），它是函数、body 到
`main.py` 的 `async with` 才跑，不构成 import 期开销。
"""

import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    """AI 推理服务生命周期管理

    单例 import 写在函数体内（规范 §3）：这是本包「零 re-export」原则的开关条款——
    写在模块级就等于把 `instance.py` 的 eager 构造重新摊给每个 import 本包的人。
    """
    from app.services.inference.instance import inference_manager

    inference_manager.start()
    logger.info("[InferenceService] Inference service started")

    try:
        yield
    finally:
        # lifespan finally 执行时 uvicorn 已 cancel 所有 WebSocket 任务，
        # 事件循环无其他等待方，直接同步调用即可。
        try:
            inference_manager.stop()
            logger.info("[InferenceService] Inference service stopped")
        except Exception:
            logger.exception("[InferenceService] Error stopping inference service")
