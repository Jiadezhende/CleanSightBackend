"""目标检测层 (L1)。

抽象：Detector / YOLODetector（detector.py，被各 workflow 继承）
处理流程（主/子进程分离）：
  主进程 StageAwareDispatcher 单线程取帧+组批+直接提交（dispatcher.py，唯一提交者）
  → RemoteInferProxy 过进程边界（infer_proxy.py）⇢[进程边界]⇢ 子进程 stage_worker.run_stages
  按 stage 路由到 StageWorker 跑前向（stage_worker.py）→ 回主进程 collector 重组 FrameInference
  → DetectionService._write_back_results 写回 ClientQueues（service.py）。

纯包标记，不做 re-export——消费方按需走深路径导入（抽象 `.detector`；管件
`.dispatcher` / `.stage_worker` / `.service` / `.infer_proxy` 不对外平铺暴露）。
"""
