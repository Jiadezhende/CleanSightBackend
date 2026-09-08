"""
step 目录落盘布局的唯一真源：目录怎么拼、文件叫什么。

`{storage_root}/{task_id}/{step_id}/` 下的全部命名约定集中在此。写侧（persistence 的
hls_strategy）与读侧（step_store 自身 / lab / inference.offline / routers）都向本模块依赖，
谁也不依赖谁。

**这条契约值得单独成模块，是因为漏改不报错**：`{track}_segment_{ts_us}.mp4` 与同名 `.idx`
一旦对不上，读侧按契约只 warning 跳过该段（那个宽容本身是对的），表现为**静默丢帧**。

**只回答「叫什么、摆在哪一层」，不回答「在磁盘的哪里」**：输出全是相对存储根的片段
（`*_name` 一段、`step_subpath` 两段），没有成员知道根在哪。故根的解析
（`store.storage_root()`，读 settings，L3）不属于本模块，由持有根的一方拼
`root / step_subpath(task_id, step_id) / segment_name(...)`。

依赖上界：stdlib only（L0）。不 import settings、不 import numpy —— 这是包外 hls_strategy /
clip_builder 能零成本直接 import 本模块的前提。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

# 轨道：raw = 原始画面（离线反查与送标用），processed = 渲染结果（回放用）
VALID_TRACKS: Tuple[str, str] = ("raw", "processed")

# 段文件名的唯一正则，读写两侧共用。
SEGMENT_PATTERN = re.compile(
    r"^(?P<track>raw|processed)_segment_(?P<ts_us>\d+)\.mp4$"
)

METADATA_NAME = "metadata.json"


def ts_to_us(ts: float) -> int:
    """段时间戳（秒）→ 文件名里的 ts_us。

    **截断而非四舍五入**，是既有落盘约定的一部分，不能改：读侧按 ts 定位段依赖
    `ts_us <= ts*1e6`（见 `segment_decoder._locate_containing_index`）。改成 round 会让
    「start_ts 恰为该段首帧」的定位无条件出错。
    """
    return int(ts * 1e6)


def step_subpath(task_id: int, step_id: int) -> Path:
    """step 目录**相对存储根**的两段路径：`{task_id}/{step_id}`。

    ⚠ **相对路径，不能直接 open**（名字叫 subpath 而非 dir 就是为了拦这个）：当成绝对路径
    用会落到进程 cwd 底下，读模式报 FileNotFoundError，写模式静默造一个野目录。必须由持有
    根的一方拼 `storage_root() / step_subpath(...)`。
    """
    return Path(str(task_id)) / str(step_id)


def segment_name(track: str, ts_us: int) -> str:
    """段文件名：{track}_segment_{ts_us}.mp4"""
    return f"{track}_segment_{ts_us}.mp4"


def sidecar_name(track: str, ts_us: int) -> str:
    """逐帧 ts sidecar 文件名：与同段 mp4 同 stem，换 .idx 后缀。

    内容是该段每帧 `frame.timestamp` 的 float64 原值数组（无 tick、无 first_ts）。
    只有 raw 轨产 sidecar —— processed 是渲染结果，离线不消费。
    """
    return f"{track}_segment_{ts_us}.idx"


def sidecar_name_for(segment_filename: str) -> str:
    """由段文件名反推同段 sidecar 文件名（读侧入口）。

    与 `sidecar_name` 是同一约定的两个方向，改一处必须同时改另一处 —— 对不上就是静默丢帧。
    """
    return str(Path(segment_filename).with_suffix(".idx"))


def init_name(track: str) -> str:
    """该轨的 fMP4 init 段：{track}_init.mp4

    首段转码时产出、整条 playlist 复用（EXT-X-MAP 声明的就是它）。两轨各有各的 init，
    共用文件名会变成「谁先转码谁定」。
    """
    return f"{track}_init.mp4"


def playlist_name(track: str) -> str:
    """该轨的 LIVE 形态播放列表：{track}_playlist.m3u8（不写 ENDLIST，VOD 由读侧动态生成）"""
    return f"{track}_playlist.m3u8"


def parse_segment_name(filename: str) -> Optional[Tuple[str, int]]:
    """段文件名 → (track, ts_us)；不匹配返回 None。"""
    m = SEGMENT_PATTERN.match(filename)
    if not m:
        return None
    return m.group("track"), int(m.group("ts_us"))
