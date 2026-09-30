"""送标流程（`service.submit_clips`）的入参出参形状。stdlib only。

    clips = validate_clips([ClipRange(0, 5_000), ClipRange(8_000, 12_000)])
    out = submit_clips(run, clips, ...)          # -> SubmitOutcome
    out.clips[i].error_code                      # ERR_* 之一，或 LS 客户端透传的 ls_unreachable / ls_auth

- `ClipOutcome` 字段与 `routers/lab.py` 的 `LabClipResultDTO` 逐一同名，router 按字段原样映射。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, NamedTuple, Optional

# 单段失败码（对外响应字面量，改名即破坏前端契约）。LS 上传失败时优先透传
# `LabelStudioTaskResult.error_code`（ls_unreachable / ls_auth / ls_bad_response），缺省才落 ERR_LS_BAD_RESPONSE。
ERR_RANGE_OUT_OF_BOUNDS = "range_out_of_bounds"
ERR_RANGE_GAP = "range_gap"
ERR_FFMPEG_FAILED = "ffmpeg_failed"
ERR_LS_BAD_RESPONSE = "ls_bad_response"


class ClipRange(NamedTuple):
    """送标区间，媒体坐标（相对该 run raw 轨媒体轴起点的 ms，= `currentTime × 1000`）。"""

    start_media_ms: int
    end_media_ms: int


@dataclass(frozen=True)
class ClipOutcome:
    """单段结果。`start_ms` / `end_ms` 等只有剪片成功（选中了段）才有值。"""

    start_media_ms: int
    end_media_ms: int
    success: bool
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    label_studio_task_id: Optional[int] = None
    duration_ms: Optional[int] = None
    size_bytes: Optional[int] = None
    n_source_segments: Optional[int] = None
    error_code: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class SubmitOutcome:
    """一次提交的结果。`job_dir` 仅在「有失败且要求保留产物」时非 None（此时目录仍在盘上）。"""

    job_dir: Optional[Path]
    clips: List[ClipOutcome]
