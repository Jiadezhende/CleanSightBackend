"""hls 域 —— `{step}/hls/` 下那一整套播放产物的定位、编解码与读写。

    from app.storage import hls

    ref = hls.insert_segment(task_id, step_id, "raw", frames)   # 交帧，拿身份键
    hls.read_segment(task_id, step_id, ref, width=W, height=H)  # 交身份键，拿帧（逆运算）
    hls.segment_path(task_id, step_id, ref)                     # 要路径时再问
    hls.delete(task_id, step_id)                                # 清掉这个 step 的整域产物

对外**三个动作 + 一组定位/枚举函数**。调用方交出内存里的 `Frame` 序列，拿回这段的身份键；
cv2 编码、ffmpeg 转 fMP4、tfdt 修补、sidecar、init、playlist、统计七件事全在域内，一件都
不出现在签名上——**像 `INSERT` 一样**：给一行数据，剩下的是存储的事。`delete` 同理，是
`DELETE ... WHERE (task, step)`：只执行，不判断该不该删。

`read_segment` / `iter_frames` 是读向的对称件：给身份键或墙钟区间，拿回**带原始 ts 的
`Frame`**。ffmpeg 解码、帧号与 sidecar 下标的对齐、两级裁剪同样一件不出签名。**只服务
raw 轨**（processed 不落 sidecar，理由见 `_decode`）。

## 读侧只出两种产出

    ① 段容器   片段元数据的查询与定位 —— 有哪些段、在哪、能不能播、多长
               SegmentRef / PlayableSegment
               list_segments        盘上全部段（含在途，不能直接喂播放器）
               list_segments_by_track  双轨只付一次 iterdir
               select_segments      按墙钟区间选段
               playable_segments    已登记可播 + EXTINF 真时长
               segment_path / init_path / sidecar_path / playlist_path / parse_*

    ② Frame    把段还原为帧 —— read_segment / iter_frames

**第三种产出要进来先问一句：它是段的元数据，还是给别人消费的装配产物？** VOD 清单是
后者，故整体不在本域（→ `app/services/utils/vod_playlist.py`）：换掉落盘格式，段容器与
`Frame` 会跟着消失，而"一份 VOD 由若干 (URI, 时长) 组成"照旧成立——方向相反，说明它
不属于这里。域对 `MediaToken`、HTTP URL、清单文本一概零认知。

写侧落盘的 `{track}_playlist.m3u8` 不受这条影响：它是本域的**产物**，读它得出的
逐段 EXTINF 由 `playable_segments` 出口，解析器 `_m3u8.durations` 保持包内私有。

## 落盘结构

    {root}/{task_id}/{step_id}/hls/
      {track}_segment_{ts_us}.mp4   段（fMP4 fragment）
      {track}_init.mp4              该轨的 init 段，首段产出、整条 playlist 复用
      {track}_playlist.m3u8         LIVE 形态清单，只追加、不写 ENDLIST
      raw_segment_{ts_us}.idx       raw 轨逐帧 ts sidecar（float64），仅离线反查用
      metadata.json                 段数 / 时长 / 首末 ts，兼作 TTL 判据
      .stage_{track}_{ts_us}/       写入事务的暂存目录，commit 后即删

两条轨（`raw` / `processed`）**各自独立**：各有各的段、init 与清单，互不引用。
detection 不在本域落盘——它由 `features` 域按帧 ts 单源写入。

## 三条时间线，一个 `eff_fps`

段的编码帧率由帧 ts 反推（`(N-1)/span`），它同时决定三个值，且必须同源：

    编码帧率   cv2.VideoWriter 按它写 N 帧 → 媒体时长 = N/fps
    EXTINF     清单里声明的段时长，= N/fps
    tfdt       该段在媒体轴上的落点，= 此前所有 EXTINF 之和 × 90000

写岔任何一个都**不报错**，表现是 hls.js 段尾停摆、总时长缩水或画面丢一截。这三个值
过去分散在写侧三处、靠注释互相约束，收进本域正是为了让它们不再跨边界传递。完整推导见
`docs/update/20260908_EXTINF_TFDT_CONTRACT.md`。

## 边界

**进本域**：文件名、目录布局、m3u8 文本、fMP4 字节、`.idx` 二进制、编解码，以及为编解码
起 cv2 / ffmpeg。
**不进**：切多长一段、失败重试几次、留多久、谁来调、并发几个——策略与编排归 `persistence`
（判据见 `app/storage/__init__.py` 的四问）。

**并发**：本域不持锁。同一 `(task, step, track)` 的写必须串行，且与该 step 的 `delete` 同序
——由调用侧的 `SerialTaskQueue` 构造，理由与失效表现见 `_write` 的「并发」一节。

**读侧能力已齐，调用点尚未迁移**：解码、段枚举、段级区间定位、可播段过滤都在域内了，但
`inference/offline` 的 `Timeline`、`traceback/segment_finder` 以及三处各自拼 VOD 清单的
调用方（`routers/traceback`、`lab/step_exporter`、`lab/clip_builder`）仍是现役，且它们读的
是 `{step}/` 平铺布局。本域的读函数只认 `{step}/hls/`，两份并存到写侧切换那一刻为止。
`metadata.json` 的读仍在域外，未承诺迁入。

## 域内分工

    types.py     本域的资源容器：SegmentRef / PlayableSegment
                 （stdlib only、不 import 同包任何模块 —— 它是子包的底）
    _layout.py   域根 / 文件名 / 轨道白名单 / 段枚举 / stage 目录（域名在此只出现一次）
    _encode.py   帧序列 → mp4v，以及 eff_fps 反推（cv2 在函数体内 import）
    _decode.py   段 → 帧序列（ffmpeg），帧号 ↔ sidecar 下标对齐，帧级裁剪
    _fmp4.py     mp4v → fMP4 fragment + init（ffmpeg），tfdt hex-patch
    _m3u8.py     LIVE 清单文本：写侧（头 / 条目 / 累计）+ 读侧（逐段 EXTINF）
    _idx.py      sidecar 的 float64 布局
    _meta.py     metadata.json 的读改写
    _write.py    写侧对外动作：insert_segment（stage → adjust → commit）/ delete
    _read.py     读侧对外动作：playable_segments / select_segments（段级裁剪在此）

本文件是 **facade**（re-export 域的公开面），不是标记型——调用方分不出 `hls` 是包还是
模块，这正是「薄域将来变重」能非破坏性升级的前提。代价是 re-export 会连带加载上面这些
实现模块，故它们的**模块级必须保持 stdlib + `app.domain`**，重依赖（cv2）一律函数体内
import，`app.storage.hls` 的导入预算才守得住。
"""

from ._decode import iter_frames, read_segment
from ._layout import (
    TRACKS,
    init_name,
    init_path,
    list_segments,
    list_segments_by_track,
    parse_init_name,
    parse_segment_name,
    playlist_path,
    segment_name,
    segment_path,
    sidecar_path,
    ts_to_us,
)
from ._read import playable_segments, select_segments
from ._write import delete, insert_segment
from .types import PlayableSegment, SegmentRef

__all__ = [
    "TRACKS",
    "PlayableSegment",
    "SegmentRef",
    "delete",
    "init_name",
    "init_path",
    "insert_segment",
    "iter_frames",
    "list_segments",
    "list_segments_by_track",
    "parse_init_name",
    "parse_segment_name",
    "playable_segments",
    "playlist_path",
    "read_segment",
    "segment_name",
    "segment_path",
    "select_segments",
    "sidecar_path",
    "ts_to_us",
]
