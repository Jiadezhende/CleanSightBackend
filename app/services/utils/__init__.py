"""services/utils —— 服务层工具：多个 service / router 都要、但不属于任何一个的通用能力。

    from app.services.utils.vod_playlist import VodEntry, render_vod

**它不是一个服务**：没有活体、没有单例、没有 `lifespan()`、不出句柄。所以「不建 service
对 service 的直接依赖」那条对它不适用——谁都可以向下依赖它，正如谁都可以向下依赖
`app/storage`。

## 边界

    可以 import   stdlib、三方、app.types、app.storage、app.settings
    不许 import   **任何兄弟 service 包**（app.services.lab / app.services.recording / …）
                  app.routers、app.db
    不许有        单例、lifespan()；模块级状态只有 metrics 的 Prometheus 指标

**「不许 import 兄弟 service」是本层存在的全部前提**。破了它，本包就成了 service → service
依赖的后门：`lab` 想调 `recording` 的东西，只要在这里加个转发函数就绕过去了，而门禁与
review 都只会看到「一个工具包」。那比三份重复实现更坏——重复至少是看得见的。
这条由 `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES` 守。

**准入判据**：这个知识有几个包会因为它变了而出错？**< 2 不进**——留在那个唯一的主人
那里。跨层用的不进这里：契约与异常进 `app/types/`，其余放 `app/` 根。

## 成员

    vod_playlist.py     VOD 形态 m3u8 的条目形状与文本渲染
    media_timeline.py   断流判定：GAP_THRESHOLD_MS / first_gap / total_gap_ms（媒体轴本身在 storage.hls）
    task_queue.py       SerialTaskQueue（单消费者串行队列）
    worker_guard.py     guarded_run（线程主循环级自愈）
    pressure.py         PressureReporter（`[PRESSURE]` 周期快照日志）
    metrics.py          Prometheus 指标

本包是**标记型** `__init__.py`（规范 §3）：纯 docstring、零 re-export，消费方走深路径。
re-export 会让整棵子树的依赖变 eager，而这里将来可能进带重依赖的工具。
"""
