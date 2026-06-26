"""
HLS 段定位

利用文件名 `{track}_segment_{ts_us}.mp4` 中的微秒时间戳 ts_us 二分定位。
keypoints JSON 同样按 `keypoints_{ts_us}.json` 命名，与 processed 段一一对应。

依赖落盘约定：
    {base_dir}/{task_id}/{step_id}/{raw|processed}_segment_{ts_us}.mp4
    {base_dir}/{task_id}/{step_id}/keypoints_{ts_us}.json
"""

import bisect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


_SEGMENT_PATTERN = re.compile(r"^(?P<track>raw|processed)_segment_(?P<ts_us>\d+)\.mp4$")
_VALID_TRACKS = ("raw", "processed")


@dataclass(frozen=True)
class SegmentRef:
    """单个 HLS 段引用。

    Attributes:
        task_id: 任务 id
        step_id: 洗消步骤 id（来自 clean_task.current_step 转 int）
        track: "raw" 或 "processed"
        filename: 段文件名（如 "processed_segment_1700000000000000.mp4"）
        ts_us: 段开始时间戳（微秒）
        path: 段文件绝对路径
        keypoints_filename: 对应 keypoints JSON 文件名（仅 processed 有效，raw 为 None）
        is_trigger: 是否为告警触发段（仅在 evidence 上下文中有意义）
    """

    task_id: int
    step_id: int
    track: str
    filename: str
    ts_us: int
    path: Path
    keypoints_filename: Optional[str] = None
    is_trigger: bool = False

    @property
    def ts_ms(self) -> int:
        """段开始时间戳（毫秒）"""
        return self.ts_us // 1000

    @property
    def ts_s(self) -> float:
        """段开始时间戳（秒，浮点）"""
        return self.ts_us / 1_000_000.0


class SegmentFinder:
    """按 (task_id, step_id) + 时间戳定位 HLS 段。

    无状态，所有方法依赖 base_dir 注入，便于测试。
    """

    def __init__(self, base_dir: Path):
        """
        Args:
            base_dir: 持久化存储根目录（对应 settings.storage_base_dir）
        """
        self._base_dir = Path(base_dir).resolve()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def task_dir(self, task_id: int, step_id: int) -> Path:
        """任务-步骤目录绝对路径：{base_dir}/{task_id}/{step_id}/"""
        return self._base_dir / str(task_id) / str(step_id)

    def list_segments(
        self,
        task_id: int,
        step_id: int,
        track: str,
    ) -> List[SegmentRef]:
        """列出某任务-步骤、某轨道下的全部段（按 ts_us 升序）。

        Args:
            task_id: 任务 id
            step_id: 洗消步骤 id
            track: "raw" 或 "processed"

        Returns:
            按时间升序的 SegmentRef 列表；目录不存在或没有匹配段时返回空列表

        Raises:
            ValueError: track 非法
        """
        if track not in _VALID_TRACKS:
            raise ValueError(f"Invalid track: {track!r}, expected one of {_VALID_TRACKS}")

        task_path = self.task_dir(task_id, step_id)
        if not task_path.exists() or not task_path.is_dir():
            return []

        refs: List[SegmentRef] = []
        for entry in task_path.iterdir():
            if not entry.is_file():
                continue
            m = _SEGMENT_PATTERN.match(entry.name)
            if not m or m.group("track") != track:
                continue
            try:
                ts_us = int(m.group("ts_us"))
            except ValueError:
                logger.warning("Skipping segment with invalid ts_us: %s", entry.name)
                continue

            keypoints_filename = (
                f"keypoints_{ts_us}.json" if track == "processed" else None
            )

            refs.append(
                SegmentRef(
                    task_id=task_id,
                    step_id=step_id,
                    track=track,
                    filename=entry.name,
                    ts_us=ts_us,
                    path=entry,
                    keypoints_filename=keypoints_filename,
                )
            )

        refs.sort(key=lambda r: r.ts_us)
        return refs

    def find(
        self,
        task_id: int,
        step_id: int,
        ts_ms: int,
        track: str,
        n_before: int = 1,
        n_after: int = 2,
    ) -> List[SegmentRef]:
        """定位包含 ts_ms 的段，并扩展前 n_before / 后 n_after 段作为上下文。

        Args:
            task_id: 任务 id
            step_id: 洗消步骤 id
            ts_ms: 目标时间戳（毫秒，与 clean_alarm.detected_at 单位一致）
            track: "raw" 或 "processed"
            n_before: 触发段之前要附带的段数
            n_after: 触发段之后要附带的段数

        Returns:
            上下文段列表（按 ts_us 升序）。其中 is_trigger=True 标记触发段。
            如果 ts_ms 早于第一段开始时间，触发段取第一段；如果没有段则返回空列表。

        算法：
            ts_us = ts_ms * 1000
            找最大的段满足 segment.ts_us <= ts_us
            该段就是"触发段"（detected_at 落在该段时间区间内）。
        """
        if n_before < 0 or n_after < 0:
            raise ValueError("n_before/n_after must be >= 0")

        all_segs = self.list_segments(task_id, step_id, track)
        if not all_segs:
            return []

        ts_us = int(ts_ms) * 1000

        # bisect_right 找到第一个 > ts_us 的位置；trigger_idx = pos - 1
        ts_list = [s.ts_us for s in all_segs]
        pos = bisect.bisect_right(ts_list, ts_us)
        trigger_idx = pos - 1

        if trigger_idx < 0:
            # ts_ms 早于第一段开始时间 → 取第一段作为最近的触发段
            trigger_idx = 0

        start = max(0, trigger_idx - n_before)
        end = min(len(all_segs), trigger_idx + n_after + 1)  # +1 因为 slice 不含 end

        result: List[SegmentRef] = []
        for i in range(start, end):
            s = all_segs[i]
            # SegmentRef 是 frozen dataclass —— 重建副本设置 is_trigger
            result.append(
                SegmentRef(
                    task_id=s.task_id,
                    step_id=s.step_id,
                    track=s.track,
                    filename=s.filename,
                    ts_us=s.ts_us,
                    path=s.path,
                    keypoints_filename=s.keypoints_filename,
                    is_trigger=(i == trigger_idx),
                )
            )
        return result

    def keypoints_path(self, task_id: int, step_id: int, ts_us: int) -> Path:
        """根据 ts_us 计算 keypoints JSON 绝对路径（不校验是否存在）"""
        return self.task_dir(task_id, step_id) / f"keypoints_{ts_us}.json"


def get_default_base_dir() -> Path:
    """取持久化默认 base_dir（与 hls_strategy 写入路径同源）。

    直读 settings.storage_base_dir 单一真源，保证读写两侧对相对路径
    的解析逻辑完全一致（相对路径都以项目根为基）。
    """
    from app.settings import settings

    return settings.storage_base_dir
