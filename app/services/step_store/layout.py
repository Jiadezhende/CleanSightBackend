"""
step 目录落盘布局的唯一真源：目录怎么拼、文件叫什么。

`{base_dir}/{task_id}/{step_id}/` 下的全部命名约定集中在此。写侧（persistence 的
hls_strategy）与读侧（step_store 自身 / lab / inference.offline / routers）都向本模块
依赖，谁也不依赖谁 —— 抽出来之前，同一套命名在写侧读侧各硬编码一次，靠"stem 长一样"
这个隐式契约互相对上。

**为什么这条契约值得单独成模块**：sidecar 的漏改不会报错。`{track}_segment_{ts_us}.mp4`
与同名 `.idx` 此前分别由写侧 f-string 拼、读侧 `with_suffix` 反推；命名一旦不一致，
读侧 `_load_sidecar` 按契约只 warning 跳过该段（那个宽容本身是对的），表现为**静默丢帧**
而非失败。

依赖上界：stdlib only（L0）。不 import settings、不 import numpy —— 任何人都该能零成本
拿到命名。存储根目录的解析在 `finder.storage_root()`，那里才碰 settings。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

# 轨道：raw = 原始画面（离线反查与送标用），processed = 渲染结果（回放用）
VALID_TRACKS: Tuple[str, str] = ("raw", "processed")

# 段文件名的唯一正则。此前有两份拷贝：读侧 finder 用 named group、写侧 hls_strategy
# 用位置 group（`_SEGMENT_FNAME_RE`），同一模式两种写法。
SEGMENT_PATTERN = re.compile(
    r"^(?P<track>raw|processed)_segment_(?P<ts_us>\d+)\.mp4$"
)

METADATA_NAME = "metadata.json"


def ts_to_us(ts: float) -> int:
    """段时间戳（秒）→ 文件名里的 ts_us。

    **截断而非四舍五入**，这是既有落盘约定的一部分，不能改：读侧按 ts 定位段时依赖
    `ts_us <= ts*1e6`（见 finder 的段级二分为何必须用 `side='right'` 再减一）。
    改成 round 会让"start_ts 恰为该段首帧"的定位无条件出错。
    """
    return int(ts * 1e6)


def step_dir(base_dir: Path, task_id: int, step_id: int) -> Path:
    """任务-步骤目录：{base_dir}/{task_id}/{step_id}/"""
    return Path(base_dir) / str(task_id) / str(step_id)


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

    与 `sidecar_name` 是同一约定的两个方向，务必保持一致 —— 它们对不上就是静默丢帧。
    """
    return str(Path(segment_filename).with_suffix(".idx"))


def init_name(track: str) -> str:
    """该轨的 fMP4 init 段：{track}_init.mp4

    首段转码时产出、整条 playlist 复用（EXT-X-MAP 声明的就是它）。两轨各有各的
    init，共用文件名会变成"谁先转码谁定"。
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
