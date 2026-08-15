"""离线段 (offline) —— 全序列动作分割与训练样例导出。

    cli (编排)  →  infer.Segmenter.segment(task, step)  →  FactLedger
                        └─ 自己按 needs 调 blocks.load(...) 取特征块

两段各司其职：`blocks/` 是**工具**（构建 / 加载 / 缓存 / 回收特征块），`infer/` 是**策略**
（吃块出事实）。编排层不认识块，块层不认识模型。

离线链路只识别稳定存储键 `(task_id, step_id)`，不接 client/CQ/在线 Operator/告警；
独立进程手动跑（见 cli.py）。

**本包顶层刻意不 re-export `OfflineSegmenter`**：那会让 `import app...offline` 顺带拖起
infer 包（进而 torch）。需要基类的按全路径 import `offline.infer.segmenter`。
"""

__all__: list[str] = []
