"""推理链路自用的 topic 声明（写回 / 时序 actor / 可视化之间的交接）。

    from app.services.inference.online.topics import LATEST_DETECTION
    sess.channel(LATEST_DETECTION).set(frame_detection)

跨模块共享的 topic 在 `app/services/session/topics.py`。
"""

from app.services.session.core import Latest, Topic

# 写回 → viz：最新一帧 FrameDetection（多流对齐的原子快照）
LATEST_DETECTION = Topic("LATEST_DETECTION", Latest)
# 时序 actor → viz：最新一轮叠字事件 List[str]
LATEST_TEMPORAL = Topic("LATEST_TEMPORAL", lambda: Latest(default=[]))
# viz 自用：上次送渲染的推理 ts（epoch 秒），去重同一推理结果
VIZ_LAST_TS = Topic("VIZ_LAST_TS", lambda: Latest(default=0.0))
