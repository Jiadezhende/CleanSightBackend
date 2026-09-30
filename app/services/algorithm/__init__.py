"""算法服务：无状态纯计算，与主流程（推理 / 录制 / 告警）无关，只被 `routers/algorithm.py` 调用。

    from app.services.algorithm import service as algorithm_service
    verdict = algorithm_service.grade_colorstrip(image_bytes, profile=None)

- `service.py`：对外接口。入参是原始字节，出参是结论 dataclass；只抛 `ValueError` /
  `KeyError`，翻成 HTTP 是 router 的活。
- 每个子包是一个自成一体的算法（参数配置、类型、实现、CLI 都在自己目录里）：
  - colorstrip: 过氧乙酸试纸色卡比色判定

**整个包不依赖任何 `app.*`**（含 `app.settings`）——上限、默认值一律写进算法子包自己的
配置文件。由 `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES` 把住。

无活体、无单例、无 `lifespan()`：没有状态可管，不挂进 `app.main` 的启动序列。

标记型 `__init__`（规范 §3）：纯 docstring、零 re-export，消费方走深路径。
re-export 会把整棵子树的重依赖变 eager，而 cv2 是禁止模块顶层 import 的 L2 依赖。
"""
