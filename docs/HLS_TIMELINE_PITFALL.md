# 踩坑记录：HLS 时长三方不一致

> **日期**：2026-05-15
> **现象上报**：lab 页面 task=1/step=1 时长显示 24s；`<video>` 元素初始 `duration=29s`，播放后跳到 `19s`；进度条组件显示 `09:19:26.784 → 09:19:51.514`（24.73s）
> **结论**：两个独立 bug 叠加 —— timeline endpoint 漏算尾段时长 + ffmpeg HLS muxer fmp4 模式吞掉 tfdt 偏移

---

## 三个数字分别来自哪里

测同一个 step (`database/1/1/`)，得到三套时长：

| 来源 | 数值 | 计算口径 |
|---|---|---|
| `GET /traceback/task/1/timeline` `.duration_ms` | **24.73s** | `(max_ts_us - min_ts_us) / 1000`，混合 raw+processed 两轨 |
| `<video>.duration` 加载完成时 | **29s** | hls.js 解析 raw playlist：`Σ EXTINF` |
| `<video>.duration` 实际播放后 | **19s** | MSE source buffer 实际能放出来的最长跨度 |

每个数字单独看都「自洽」，但三方根本不对齐。

---

## Bug A：timeline endpoint 用 wall-clock 文件名时间戳

[`app/routers/traceback.py`](../app/routers/traceback.py) 的 `_step_duration_ms` 旧实现：

```python
all_ts = [s.ts_us for s in (raw_segs + proc_segs)]
start_us = min(all_ts)
end_us = max(all_ts)
return start_us // 1000, end_us // 1000, (end_us - start_us) // 1000
```

`SegmentRef.ts_us` 是从文件名解析的微秒时间戳，**对应段首帧的 wall-clock**。
取 `max(ts_us)` 拿到的是**最后一段的起点**，不是它的终点 —— 最后一段自身长度 (10s 左右) 直接没算进去。

更阴险的是：raw / processed 两轨被混在一起取 min/max。

实测 db/1/1：

- raw 段 ts_us = 26.784s, 36.772s, 46.773s（最后一段 EXTINF=9.333s）
- processed 段 ts_us = 26.784s, 51.514s（最后一段 EXTINF=2.850s，因为推理队列降采样）

`max(ts_us)` 落在 processed 第二段起点 `51.514s`。这既不是 raw 真实末端 (`46.773+9.333=56.106s`)，也不是 processed 真实末端 (`51.514+2.850=54.364s`)，是一个**中间夹缝**值，跟用户能看到的任何一个真实时长都对不上 —— 巧的是看起来还挺像那么回事的 24.73s，所以这个 bug 在 commit `1ed3571` 修「统一 hls 段时间戳」时被忽略了。

### 修法

读 `raw_playlist.m3u8` / `processed_playlist.m3u8` 拿 EXTINF，按 `end_ms = max(seg.ts_us + EXTINF)` 算。
复用现成的 `_parse_existing_playlist` 函数。

---

## Bug B：ffmpeg HLS muxer + fmp4 + `-start_number 0` 会把 tfdt 清零

这个比较深，连续踩了两次。

### 背景

hls.js 在 VOD 模式下，连续播放跨 fragment 时**依赖每个 fragment 的 `moof/traf/tfdt.baseMediaDecodeTime`** 来决定它在 MSE 时间线上落在哪。
要让段 N 的 tfdt = `Σ(段 0..N-1 的媒体时长)`，否则三个 fragment 都从 t=0 起，append 进 source buffer 会互相覆盖。

### 第一坑（已在 commit `16c532e` 修过）

最早写法是文件名 ts_us 差。但 wall-clock 抖动 ±500ms，跟 cv2.VideoWriter 写出来的真实媒体时长对不上 → hls.js 段尾停摆。

### 第二坑（commit `1ed3571` 想修但没真修）

后续把 EXTINF 改成 `len(frames)/fps`，`_ts_offset_seconds` 改成读 playlist 累计 EXTINF，理论上三套时间线（EXTINF、tfdt、fragment 媒体时长）就锚定到同一真值了。

ffmpeg 命令里靠 `-output_ts_offset` 把 tfdt 写进 fragment：

```bash
ffmpeg -i input.mp4 \
  -output_ts_offset 20.000 \
  -hls_segment_type fmp4 \
  -start_number 0 \
  -f hls output.m3u8
```

commit message 写得明明白白「三套时间线锚定到同一真值」。然而**实际产物的 tfdt 还是 0**。

确认方式（不需要装 mp4box，纯 Python）：

```python
import struct
data = open('raw_segment_xxx.mp4', 'rb').read()
i = 0
while i + 8 <= len(data):
    size = struct.unpack('>I', data[i:i+4])[0]
    typ = data[i+4:i+8]
    if typ in (b'moof', b'traf'):
        i += 8                        # 容器盒，下钻
        continue
    if typ == b'tfdt':
        ver = data[i+8]
        fmt = '>Q' if ver == 1 else '>I'
        width = 8 if ver == 1 else 4
        t = struct.unpack(fmt, data[i+12:i+12+width])[0]
        print('baseMediaDecodeTime =', t)
    i += size
```

实测 db/1/1 三个 raw fragment 全是 `baseMediaDecodeTime = 0`。

### 为什么 `-output_ts_offset` 失效

ffmpeg 8.x 的 HLS muxer 在 fmp4 模式 + `-start_number 0` 下，会把内部段计数器清零，
并**重置** muxer 内部的累计 PTS 基线 —— `-output_ts_offset` 算的是输出 PTS 偏移，但被 HLS muxer 重新归零。

试过的等价方案，**全部无效**：

| 方案 | 现象 |
|---|---|
| `-output_ts_offset 20` | tfdt 仍为 0 |
| `-itsoffset 20 -i input -copyts` | tfdt 仍为 0 |
| `-muxdelay 20 -muxpreload 20` | tfdt 仍为 0 |
| `-fflags +genpts -vsync passthrough` 各种组合 | tfdt 仍为 0 |

每个 ffmpeg 进程都是「全新的 muxer」，HLS muxer 把它当作新的 playlist 起点。除非塞进同一个 ffmpeg 进程里 batch 编码（破坏现有「每段独立子进程」的流水线），否则没法靠 ffmpeg 参数解决。

### 修法（绕过 ffmpeg）

转码完直接 **hex-patch tfdt box 的 `baseMediaDecodeTime` 字段**：

- fMP4 box 结构稳定：`moof → traf → tfdt`，找到偏移改 8 字节大端整数即可
- 纯 metadata 改写，不动媒体数据，没有再编码代价
- timescale 从同 step 共享的 `init.mp4` 的 `moov/trak/mdia/mdhd` 读，首次解析后缓存到 `.hls_timescale` 文本文件

[`app/services/persistence/strategies/hls_strategy.py`](../app/services/persistence/strategies/hls_strategy.py) 新增的辅助方法：

- `_iter_boxes` / `_find_box_path` —— 通用 ISO BMFF box 树扫描
- `_read_timescale_from_init` —— 从 init.mp4 的 mdhd 读 timescale（v0/v1 双兼容）
- `_get_or_cache_timescale` —— `.hls_timescale` 缓存读写
- `_patch_fragment_tfdt` —— 改 fragment 的 baseMediaDecodeTime（v0/v1 双兼容）

`_transcode_to_fmp4_segment` 里删掉 `-output_ts_offset`，转码完 `os.replace` 之后调
`_patch_fragment_tfdt(path, int(ts_offset * timescale))`。

---

## 三方对齐验证

修完之后，db/1/1 三方时长应该统一为 **29.32s**（差 12ms 是 playlist EXTINF 写到 3 位小数 `.3f` 的尾数精度损失，UI 显示无感）：

```bash
# timeline endpoint
curl http://localhost:8000/traceback/task/1/timeline?step_id=1 | jq '.duration_ms'
# → 29321（旧值 24729）

# 新录制段 tfdt
python -c "from pathlib import Path; ..."  # 见上文 snippet
# → 0, 153600, 307200（旧值全 0）

# lab 页面顶部「时长 29s」、进度条右端 09:19:56.106
# <video>.duration 初始 29.333，播放至末尾不再跳变到 19
```

---

## 历史段处理

db/1/1 这三段是 commit `18f43c2` 之后录的（同日凌晨），但 tfdt 仍坏 —— 说明 commit `1ed3571` 的「修复」没实际生效。
这次新增的 hex-patch 只跑在生产路径（新落盘的段），**历史段保留现状**。

如果需要修历史段，写一个独立迁移脚本扫 `database/*/*/` 的所有 `*_segment_*.mp4`：

1. 解析每个 step dir 的 `{track}_playlist.m3u8` 拿到段顺序和 EXTINF
2. 读 step dir 的 `init.mp4` 拿 timescale
3. 对每段算 `cumsum(EXTINF before)` 当 baseMediaDecodeTime
4. 调 `HLSPersistenceStrategy._patch_fragment_tfdt`

参考 `scripts/transcode_segments_to_h264.py` 的目录扫描骨架。

---

## 经验

1. **commit message 说「修复了 X」≠ X 真的修了** —— 拿真实产物验。看 m3u8 文件、解 fmp4 box，别只看 ffmpeg 进程退出码 0
2. **三个时长不一致时，每个数字单独看都自洽** —— 必须对照根因（媒体数据本身、playlist 元数据、播放器渲染）逐层 reconcile
3. **ffmpeg HLS muxer + fmp4 的 tfdt 不可外部注入** —— 8.x 实测如此。需要 tfdt 累计偏移就 hex-patch，别再试 ffmpeg flag 组合
4. **ISO BMFF box 解析不需要第三方库** —— `struct.unpack('>I', ...)` + 递归就够用，比引入 `pymp4` / `mp4parser` 之类轻量
