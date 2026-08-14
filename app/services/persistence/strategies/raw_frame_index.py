"""raw 段逐帧索引 sidecar —— 只回答「哪些帧、按什么顺序进了这个段」。

**为什么需要**：`features.jsonl` 的 `ts` 与 raw 帧 ts 严格同源同值，故「ts → 哪个段」精确
（段文件名带段首 ts）。但段内 N 帧是以 `eff_fps = (N-1)/span` 反推的**合成 CFR** 写进 mp4 的
（见 hls_strategy._persist_raw_segment），**逐帧真实 ts 写完即丢**，于是「段内第几帧」只能按
平均帧率反推、抖动会在段内累积。本 sidecar 把那份有序 ts 表留下来，使反查恢复为精确：

    ts → 定位段 → 在 frame_ts 里二分得 ordinal → 顺序解码取第 ordinal 帧

**刻意只记 `frame_ts`**（不记 eff_fps / 段时长 / timescale）：那些是容器层参数，正在被
docs/update/20260813_HLS_SEGMENT_TIMESCALE_FIX.md 与 20260813_HLS_WALLCLOCK_TIMELINE_REQUIREMENTS.md
重写；记下来就是第二份会漂移的真源。读侧取帧一律「顺序解码数第 i 帧」，不靠 PTS/eff_fps 反算——
于是段内是合成 CFR 还是真实 PTS、时基怎么修、封段触发怎么改，本映射都成立。

同理本模块**独立于 hls_strategy**（那边只插一行调用），把两篇改造的 merge 冲突面压到一行。

契约：
- `frame_ts[i]` == 该段写入的第 i 帧的源 ts，长度 == 段实际写入帧数，顺序 == 写入顺序；
- 只对 raw 轨写（取证职责在 raw，见墙钟需求 §二决策 1）；
- best-effort：写失败只告警不抛，落盘主链路可用性优先（与 feature/store.py 同款取舍）；
- 合成产物（未来的黑屏段）无源帧，不写 sidecar；读侧「无 sidecar」= 该区间无像素证据。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from app.domain.frame import Frame

logger = logging.getLogger(__name__)

# sidecar 与段同目录同名、仅换后缀：raw_segment_{ts_us}.mp4 → raw_segment_{ts_us}.idx.json。
# 一段一文件 → 随 step 目录被 TTL 连带回收，无独立生命周期。
_INDEX_SUFFIX = ".idx.json"


def index_path_for(segment_path: Union[str, Path]) -> Path:
    """段文件路径 → 其 sidecar 路径（读写两侧共用，避免各拼一次）。"""
    path = Path(segment_path)
    return path.with_name(path.stem + _INDEX_SUFFIX)


# ── frames ↔ 磁盘 record 的对称映射（一对逆运算紧挨放置，同 feature/store.py 风格）──────


def build_frame_index(frames: Sequence[Frame]) -> Dict[str, Any]:
    """帧序列 → sidecar record（逆运算 read_frame_index）。

    只取 `Frame.timestamp`，按传入顺序原样保留——调用方传的就是写进 VideoWriter 的那批帧、
    那个顺序，故不排序、不去重（重排会直接破坏 ordinal 对应关系）。
    """
    return {"frame_ts": [float(f.timestamp) for f in frames]}


def read_frame_index(segment_path: Union[str, Path]) -> Optional[List[float]]:
    """段文件路径 → 该段的有序帧 ts 表（build_frame_index 的逆）。

    返回 None 表示**没有可用索引**（sidecar 不存在 / 损坏 / 格式不符），调用方据此把该段
    整体判为不可精确取帧，而不是退化成近似反推。
    """
    path = index_path_for(segment_path)
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        frame_ts = rec["frame_ts"]
        return [float(x) for x in frame_ts]
    except Exception as e:  # 损坏 / 缺键 / 非数值：宁可判为无索引，不给出错误的 ordinal
        logger.warning("[RawFrameIndex] 索引不可用 %s: %s", path, e)
        return None


def write_frame_index(segment_path: Union[str, Path], frames: Sequence[Frame]) -> bool:
    """落一份该段的 sidecar。tmp + os.replace 原子替换；best-effort，失败只告警。

    **在目录锁之外调用**：sidecar 是段私有文件，与 playlist append / metadata 更新无竞争，
    也不参与 tfdt 累计；而那把锁内的三段正是在制 HLS 改造要重写的部分。

    Args:
        segment_path: 段文件路径（`raw_segment_{ts_us}.mp4`）
        frames: 写进该段的帧，顺序须与 VideoWriter 写入顺序一致

    Returns:
        是否成功落盘（供调用方计数/断言；主链路不依赖返回值）
    """
    if not frames:
        return False
    path = index_path_for(segment_path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        payload = json.dumps(build_frame_index(frames), ensure_ascii=False)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:  # best-effort：不阻断落盘主链路
        logger.warning("[RawFrameIndex] sidecar 落盘失败 %s: %s", path, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
