"""
HLS 段定位

利用文件名 `{track}_segment_{ts_us}.mp4` 中的微秒时间戳 ts_us 二分定位。

依赖落盘约定：
    {base_dir}/{task_id}/{step_id}/{raw|processed}_segment_{ts_us}.mp4
"""

import bisect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
        is_trigger: 是否为告警触发段（仅在 evidence 上下文中有意义）
    """

    task_id: int
    step_id: int
    track: str
    filename: str
    ts_us: int
    path: Path
    is_trigger: bool = False

    @property
    def ts_ms(self) -> int:
        """段开始时间戳（毫秒）"""
        return self.ts_us // 1000

    @property
    def ts_s(self) -> float:
        """段开始时间戳（秒，浮点）"""
        return self.ts_us / 1_000_000.0


@dataclass(frozen=True)
class StepRef:
    """单个已落盘 step 的摘要（清单类接口用，不含逐段明细）。

    Attributes:
        task_id: 任务 id
        step_id: 洗消步骤 id
        tracks: 该 step **实际落盘**的轨道，按 ("raw", "processed") 顺序
        first_ts_us: 双轨并集里最早的段开始时间戳（微秒）
        last_ts_us: 双轨并集里最晚的**段开始**时间戳（微秒）——注意不是任务结束时刻，
            差一个段长；精确时长由 traceback 的 timeline 按 playlist EXTINF 算
    """

    task_id: int
    step_id: int
    tracks: Tuple[str, ...]
    first_ts_us: int
    last_ts_us: int


def _dir_name_to_int(name: str) -> Optional[int]:
    """目录名转 int；非数字（如 `.lab_exports`）返回 None。"""
    try:
        return int(name)
    except (TypeError, ValueError):
        return None


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

    def _scan_step_dir(self, task_id: int, step_id: int) -> Dict[str, List[SegmentRef]]:
        """单次 iterdir 扫出该 step 目录下按轨道分组的段（各轨内按 ts_us 升序）。

        双轨枚举（清单接口要 tracks）只付一次目录遍历；单轨调用方走 `list_segments`。
        目录不存在时返回各轨空列表。
        """
        by_track: Dict[str, List[SegmentRef]] = {t: [] for t in _VALID_TRACKS}

        task_path = self.task_dir(task_id, step_id)
        if not task_path.exists() or not task_path.is_dir():
            return by_track

        for entry in task_path.iterdir():
            if not entry.is_file():
                continue
            m = _SEGMENT_PATTERN.match(entry.name)
            if not m:
                continue
            try:
                ts_us = int(m.group("ts_us"))
            except ValueError:
                logger.warning("Skipping segment with invalid ts_us: %s", entry.name)
                continue

            track = m.group("track")
            by_track[track].append(
                SegmentRef(
                    task_id=task_id,
                    step_id=step_id,
                    track=track,
                    filename=entry.name,
                    ts_us=ts_us,
                    path=entry,
                )
            )

        for refs in by_track.values():
            refs.sort(key=lambda r: r.ts_us)
        return by_track

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

        return self._scan_step_dir(task_id, step_id)[track]

    def list_steps(self, task_id: int) -> List[StepRef]:
        """列出该 task 下**已落盘**的 step 摘要（按 step_id 升序）。

        一个 step 目录只要两轨都没有段就丢弃——目录建了但没写成段（起流即失败）
        对回放没有意义，清单不该把它露给前端点开黑屏。

        Returns:
            StepRef 列表；task 目录不存在或无任何段时返回空列表
        """
        task_root = self._base_dir / str(task_id)
        if not task_root.exists() or not task_root.is_dir():
            return []

        steps: List[StepRef] = []
        for entry in task_root.iterdir():
            if not entry.is_dir():
                continue
            step_id = _dir_name_to_int(entry.name)
            if step_id is None:
                continue

            by_track = self._scan_step_dir(task_id, step_id)
            tracks = tuple(t for t in _VALID_TRACKS if by_track[t])
            if not tracks:
                continue

            all_ts = [s.ts_us for t in tracks for s in by_track[t]]
            steps.append(
                StepRef(
                    task_id=task_id,
                    step_id=step_id,
                    tracks=tracks,
                    first_ts_us=min(all_ts),
                    last_ts_us=max(all_ts),
                )
            )

        steps.sort(key=lambda s: s.step_id)
        return steps

    def list_task_ids(self) -> List[int]:
        """列出存储根目录下的 task 目录（升序）。

        只认数字目录名——`.lab_exports` 等非任务目录跳过。不校验目录内是否真有段
        （那要深扫，交给调用方按需 `list_steps`）。
        """
        if not self._base_dir.exists() or not self._base_dir.is_dir():
            return []

        task_ids = [
            tid
            for entry in self._base_dir.iterdir()
            if entry.is_dir() and (tid := _dir_name_to_int(entry.name)) is not None
        ]
        task_ids.sort()
        return task_ids

    def list_task_ids_by_recency(self) -> List[int]:
        """按「最近有段落盘」倒序列出 task 目录——**廉价粗排，仅供挑候选**。

        排序键 = max(该 task 下各 step 目录的 mtime)。段文件写入会更新其所在
        step 目录的 mtime，故该值 ≈ 最后一段落盘时刻。只 stat 目录、不进目录读段
        文件，成本 O(目录数) 而非 O(总段文件数)。

        近似性是有意的：mtime 只用来决定「先深扫谁」，绝不对外当时间戳用——
        对外的段时间一律取 `list_steps()` 的真实 ts_us。无 step 子目录的 task
        排序键取 0（排最后），但仍保留在结果里，由调用方深扫时丢弃。
        """
        if not self._base_dir.exists() or not self._base_dir.is_dir():
            return []

        keyed: List[Tuple[float, int]] = []
        for entry in self._base_dir.iterdir():
            if not entry.is_dir():
                continue
            task_id = _dir_name_to_int(entry.name)
            if task_id is None:
                continue

            mtimes = [
                step.stat().st_mtime
                for step in entry.iterdir()
                if step.is_dir() and _dir_name_to_int(step.name) is not None
            ]
            keyed.append((max(mtimes) if mtimes else 0.0, task_id))

        keyed.sort(reverse=True)  # mtime 降序；同 mtime 时 task_id 大者优先
        return [task_id for _, task_id in keyed]

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
                    is_trigger=(i == trigger_idx),
                )
            )
        return result


def get_default_base_dir() -> Path:
    """取持久化默认 base_dir（与 hls_strategy 写入路径同源）。

    直读 settings.storage_base_dir 单一真源，保证读写两侧对相对路径
    的解析逻辑完全一致（相对路径都以项目根为基）。
    """
    from app.settings import settings

    return settings.storage_base_dir
