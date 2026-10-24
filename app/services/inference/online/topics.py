"""推理链路自用的 topic 声明（写回 / 时序 actor / 可视化之间的交接）。

    from app.services.inference.online.topics import LATEST_DETECTION
    sess.channel(LATEST_DETECTION).set(frame_detection)

跨模块共享的 topic 在 `app/services/session/topics.py`。
"""

from app.services.session.core import Latest, TimeWindow, Topic

# `/task` 的 signals_10s 固定聚合最近 10s，帧窗保留时长不低于它
SIGNALS_WINDOW_SEC = 10.0

# 写回 → viz：最新一帧 FrameDetection（多流对齐的原子快照）
LATEST_DETECTION = Topic("LATEST_DETECTION", Latest)
# 时序 actor → viz：最新一轮叠字事件 List[str]
LATEST_TEMPORAL = Topic("LATEST_TEMPORAL", lambda: Latest(default=[]))
# 写回 → actor、`/task` 信号汇总：帧级 FrameDetection 按 ts 保留。
# 保留时长 = max(10s, 本 run 各算子最大感受野)，start_workflow 实例化算子后 extend_span 放宽。
# 每 run 写回按 ts 非递减（单提交者 FIFO + 推理子进程串行 + 单收集线程）。
DETECTION_WINDOW = Topic("DETECTION_WINDOW", lambda: TimeWindow(span=SIGNALS_WINDOW_SEC))
# viz 自用：上次送渲染的推理 ts（epoch 秒），去重同一推理结果
VIZ_LAST_TS = Topic("VIZ_LAST_TS", lambda: Latest(default=0.0))
