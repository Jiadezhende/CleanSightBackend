"""目标检测层 (L1)。

抽象：Detector / YOLODetector（detector.py）
处理流程：StageAwareDispatcher 取帧分组（dispatcher.py）→ MultiModelWorkerPool 模型池（pool.py）
         → ModelWorkerService worker 写回（service.py）

纯包标记，不做 re-export——消费方按需走深路径导入（抽象 `.detector`；管件
`.dispatcher` / `.pool` / `.service` 不对外平铺暴露）。
"""
