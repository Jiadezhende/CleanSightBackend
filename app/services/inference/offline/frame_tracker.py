from __future__ import annotations

from typing import Iterator

from app.domain.frame import Frame
from app.storage import hls


class FrameTracker:
    """按 ts 反查原始帧。解码与裁剪全下沉到 `app.storage.hls`，本类只剩位级配对。

    **只服务 raw 轨**，故没有 `track` 参数：`processed` 是画完框的渲染结果、按契约不落
    sidecar，给不出带墙钟 ts 的帧（见 `app/storage/hls/_decode.py` 的「只服务 raw 轨」）。
    """

    def __init__(self, task_id: int, step_id: int):
        self._task_id = task_id
        self._step_id = step_id

    def find(self, timestamps: list[float], width: int, height: int) -> Iterator[Frame]:
        """按 ts 反查帧。

        产出顺序为 **ts 升序**，不保证与入参同序 —— 调用方按 `frame.timestamp`
        对号入座，勿按位置。重复 ts 按重数各产出一帧（同一 Frame 对象）。

        `timestamps` 必须**位级等于** sidecar 里的帧 ts，即取自同一 run 的
        detections.jsonl / `inference.read_detections()`（两侧同源同值，见 `app.domain.temporal`
        的时间轴约束）。任何精度中转（float32、重新格式化）都会 ValueError —— 这里不做
        近似匹配：ts 是帧的身份，配错帧比报错更坏。
        """
        if not timestamps:
            return
        sorted_timestamps = sorted(float(t) for t in timestamps)

        idx = 0
        for frame in hls.iter_frames(
            self._task_id,
            self._step_id,
            width=width,
            height=height,
            start_ts=sorted_timestamps[0],
            end_ts=sorted_timestamps[-1],
        ):
            # while 而非 if：重复 ts 在同一帧上连续消费掉
            while idx < len(sorted_timestamps) and frame.timestamp == sorted_timestamps[idx]:
                yield frame
                idx += 1
        if idx < len(sorted_timestamps):
            raise ValueError(f"未找到 ts={sorted_timestamps[idx]!r} 对应帧")
