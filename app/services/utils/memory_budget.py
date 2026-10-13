"""内存容量规划：按配置估 N 路并发的最坏帧缓存占用，供启动自检与 MemAvailable 比对。

    plan = MemoryPlan(streams=15, frame_bytes=640 * 480 * 3, ca_maxlen=600, ca_ready_maxlen=30,
                      segment_len=300, recording_budget_bytes=8 << 30, baseline_bytes=6 << 30)
    est = estimate(plan)
    est.worst            # 最坏场景 + 进程基线（字节）
    read_mem_available() # Linux 读 /proc/meminfo，其它平台 None

几种停滞不同时发生（sweeper 活着时 ca 队列持续被拉空；sweeper 停了录像队列也不再进段），
故最坏 = 各场景取最大，而不是相加。公式依据见 docs/update/20261013_MEMORY_BUDGET.md。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MemoryPlan:
    streams: int                 # 规划并发路数
    frame_bytes: int             # 单帧字节（W × H × 3）
    ca_maxlen: int               # ca_raw / ca_processed 各自上限（帧）
    ca_ready_maxlen: int         # ca_ready 上限（帧）
    segment_len: int             # 一段帧数（raw / processed 同口径）
    recording_budget_bytes: int  # 录像段队列在途字节上限
    baseline_bytes: int          # 帧缓存之外的进程基线


@dataclass(frozen=True)
class MemoryEstimate:
    normal: int           # 稳态：每路 raw + processed 各攒满一段（段边界对齐时的峰值）
    recording_stall: int  # 转码停滞：稳态 + 录像队列预算打满
    inference_stall: int  # 推理停滞：稳态 + ca_ready 打满（与 ca_raw 不再共享的帧）
    sweeper_stall: int    # sweeper 停滞：ca_raw / ca_processed 打满 + ca_ready 打满
    baseline: int

    @property
    def worst(self) -> int:
        return max(self.recording_stall, self.inference_stall, self.sweeper_stall) + self.baseline


def estimate(plan: MemoryPlan) -> MemoryEstimate:
    fb = plan.frame_bytes
    normal = plan.streams * 2 * plan.segment_len * fb
    ready = plan.streams * plan.ca_ready_maxlen * fb
    return MemoryEstimate(
        normal=normal,
        recording_stall=normal + plan.recording_budget_bytes,
        inference_stall=normal + ready,
        sweeper_stall=plan.streams * 2 * plan.ca_maxlen * fb + ready,
        baseline=plan.baseline_bytes,
    )


def read_mem_available(path: str = "/proc/meminfo") -> Optional[int]:
    """`MemAvailable`（字节）；文件不存在（非 Linux）或无该字段时返回 None。"""
    try:
        with open(path, "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None
