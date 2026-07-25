"""可视化层 (Viz)。

worker.py：VisualizationWorker —— 定时拉取快照 + 渲染循环
pool.py  ：VisualizationWorkerPool —— 线程启停管理
visualizer.py：FixedVisualizer —— 固定渲染器（纯渲染，无线程/队列）

纯包标记，不做 re-export——消费方按需走深路径导入（`.visualizer` / `.worker` /
`.pool`，管件不对外平铺暴露）。
"""
