"""算法层：无状态纯计算，与业务编排无关。

每个子包是一个自成一体的算法：参数配置、类型、实现、CLI 都在自己目录里，
**不依赖任何 `app.*`**（含 `app.settings`）——上限、默认值一律写进子包自己的
配置文件，不去顶层 `config/` 或 `settings.py` 取。
这条由 `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES` 把住。

为什么不是 `app/services/<x>/`：算法无活体、无状态、谁都可以向下依赖它。
放进某个 service 包，别处要用就成了规范 §3 禁止的 service → service 依赖。

现有子包：
- colorstrip: 过氧乙酸试纸色卡比色判定

标记型 `__init__`（规范 §3）：纯 docstring、零 re-export，消费方走深路径
（`from app.algorithm.colorstrip import grader`）。re-export 会把整棵子树的
重依赖变 eager，而 cv2 是禁止模块顶层 import 的 L2 依赖。
"""
