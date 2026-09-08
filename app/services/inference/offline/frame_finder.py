"""
按 ts 反查原始帧：离线链路对 `hls.frames`（step_store 的区间解码出口）的消费策略。

本模块只有一件事——**把「区间取帧」变成「按 ts 对号取帧」**，两者的失败契约相反：

    hls.frames         区间扫描，宽容：缺 sidecar 跳过该段、空区间返回空
    FrameFinder.find   点查，严格：任一 ts 配不上就 ValueError

宽容留在解码层（缺一段的索引不该让前后所有段一起读不了），严格留在这层
（ts 是帧的身份，配错帧比报错更坏）。这也是两者分居两个模块的理由。
"""

from __future__ import annotations

from functools import partial
from typing import Callable, Iterator, List, Optional

from app.domain.frame import Frame
from app.services.step_store import hls


class FrameFinder:
    """按 ts 反查帧。ts 必须位级等于 sidecar 里的帧 ts。"""

    def __init__(
        self,
        task_id: int,
        step_id: int,
        track: str = "raw",
        decoder: Optional[object] = None,
    ):
        """
        Args:
            decoder: 解码源，需有 `iter(start_ts, end_ts, width, height)`。不传则走
                `hls.frames` —— 落盘定位归 step_store，本模块只管对号入座。
                注入口是给测试用的 seam（把 ffmpeg 换成按 sidecar 合成帧），不必
                monkeypatch 模块属性。
        """
        if decoder is not None:
            self._frames: Callable[..., Iterator[Frame]] = decoder.iter  # type: ignore[attr-defined]
        else:
            self._frames = partial(hls.frames, task_id, step_id, track)

    def find(
        self, timestamps: List[float], width: int, height: int
    ) -> Iterator[Frame]:
        """按 ts 反查帧。

        产出顺序为 **ts 升序**，不保证与入参同序 —— 调用方按 `frame.timestamp`
        对号入座，勿按位置。重复 ts 按重数各产出一帧（同一 Frame 对象）。

        `timestamps` 必须**位级等于** sidecar 里的帧 ts，即取自同一 run 的
        features.jsonl / `FeatureStore.load()`（两侧同源同值，见 store.py 的帧对齐
        契约）。任何精度中转（float32、重新格式化）都会 ValueError —— 这里不做
        近似匹配：ts 是帧的身份，配错帧比报错更坏。
        """
        if not timestamps:
            return
        sorted_timestamps = sorted(float(t) for t in timestamps)

        idx = 0
        for frame in self._frames(
            sorted_timestamps[0], sorted_timestamps[-1], width, height
        ):
            # while 而非 if：重复 ts 在同一帧上连续消费掉
            while (
                idx < len(sorted_timestamps)
                and frame.timestamp == sorted_timestamps[idx]
            ):
                yield frame
                idx += 1
        if idx < len(sorted_timestamps):
            raise ValueError(f"未找到 ts={sorted_timestamps[idx]!r} 对应帧")
