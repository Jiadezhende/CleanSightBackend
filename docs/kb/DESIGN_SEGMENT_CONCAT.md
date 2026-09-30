> 更新时间：2026-09-30
> 依据来源：实测（ffmpeg n7.1.4-9，Windows）+ 代码分析
> 可信级别：exit code、产物大小、时长、帧数、日志原文均为实测值，复现见 §6；标「待核验」的是未在本仓库验证的推导

# 分段 fMP4 拼接：选路看两条轴，验收看内容

盘上是 `{track}_init.mp4` + N 个 fragment，要把它们变成一条连续流交给 ffmpeg（导出、裁剪、解帧）：

```text
§3 选落盘容器   回放端是 MSE → 只能 B1「fragment + 共用 init」
§4 选拼接路径   B1 下四条候选（①②③③'），`-f concat` 全家结构上不可能
§5 验收产物     三种失败 exit 0 或挂死，骗过 returncode 判据；坏段静默截短只能靠比对时长抓
```

§3、§4 的答案都由 §2 的两条轴推出。浏览器播放清单那一类属 HLS 协议本身，见
[DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)。

---

## 1. fragment 自己不带 codec 参数

```text
init.mp4     ftyp + moov               有头、没画面（SPS/PPS、timescale、trex 在这里）
seg_*.m4s    styp + sidx + moof/mdat   有画面、没头
```

整轨共用一份 `moov`，单独打开任何一个 fragment 都解不出它是什么编码。

---

## 2. 两条正交的轴

```text
轴 1  段能否被单独 demux 出完整 codec 参数？   → 决定 `-f concat` demuxer 能不能用
轴 2  段能否按字节拼成一个整体（允许前置共用头）？ → 决定 `concat:` / `concatf:` 协议能不能用

格式                  轴 1 独立 demux   轴 2 字节可拼
─────────────────────────────────────────────────────────────
普通 MP4                 ✅              ❌  多份 moov 拼一起是垃圾
自含式 fMP4 / CMAF       ✅              ❌  同上
MPEG-TS                  ✅              ✅  无唯一全局头
fMP4 fragment + init     ❌              ✅  前置 init 即可      ← 本仓库
```

`-f concat` 要「每个条目自解释」，`concat:` 要「拼起来之后整体自解释」。`concat:init|seg` 拼出的字节流
在上层 probe 为 `mov,mp4`，`moov` 与 `moof` 在同一个轨道表里相遇。

### 两个 `concat` 注册在不同层

```text
协议层     file: / pipe: / concat: / concatf: / http:   给字节         ← 轴 2
demuxer 层 mov,mp4 / hls / concat                       把字节解成包   ← 轴 1
解码层     h264 / hevc / ...                            把包解成帧
```

- **协议 `concat:` / `concatf:`**：把多个子 URL 的字节首尾相接，对上层透明。
- **demuxer `concat`**：解析纯文本清单（`ffconcat version 1.0`），对每个条目**开一个全新的
  `AVFormatContext`**，是调度器而不是容器。

### 轴 1 判死 `-f concat`：头不跨条目传递

`-loglevel debug`（清单为 `init.mp4` + `seg0.m4s`）直接显示两个不同的 context 指针：

```text
[AVFormatContext @ ...755b40] Opening 'init.mp4' for reading          ← context A
[mov,mp4 @ ...755b40] After avformat_find_stream_info() ... frames:0
[AVFormatContext @ ...764880] Opening 'seg0.m4s' for reading          ← context B
[mov,mp4 @ ...764880] could not find corresponding track id 1
[mov,mp4 @ ...764880] could not find corresponding trex (id 1)
[mov,mp4 @ ...764880] trun track id unknown, no tfhd was found
[mov,mp4 @ ...764880] error reading header
[concat @ ...f4900]   Impossible to open 'seg0.m4s'
```

`moov` 里的 `trak` 轨道表、`stsd/avcC` 的 SPS/PPS、`mvex/trex` 默认 sample 值全部随 context A 作废，
fragment 的 `tfhd` 在 context B 里找不到 track → `trun` 无宿主 → `INVALIDDATA`。**把 `init.mp4` 列进清单
第一行也没用**，只是多开一个 context（后果见 §5.1）。

---

## 3. 落盘容器只能选 B1

```text
A1  普通 MP4          ftyp + moov + mdat             段自包含
A2  自含式 fMP4/CMAF  ftyp + moov + moof + mdat      每段自带 init（实测 827 B）
A3  MPEG-TS           PAT/PMT + 带内 SPS/PPS 周期重复
B1  fMP4 fragment + {track}_init.mp4   styp + sidx + moof + mdat   ← 现役

                   hls.js / MSE     concat: 协议    -f concat      HLS 清单
─────────────────────────────────────────────────────────────────────────────
A1  普通 MP4       ❌ 实测           ❌ 待核验        ✅ 待核验       ? 待核验
A2  自含式 CMAF    ? 待核验          ❌ 待核验        ✅ 待核验       ? 待核验
A3  MPEG-TS        ✅ 客户端转封装   ✅ 待核验        ✅ 待核验       ✅ 待核验
B1  fragment+init  ✅ 实测           ✅ 实测          ❌ 实测         ✅ 实测
```

A1 的 MSE ❌ 见 `app/storage/hls/_fmp4.py` 模块 docstring（hls.js 当段播报 `fragParsingError`）。
回放端是 hls.js / MSE，A1 出局；A2 未验证且每段多付一份 moov。B1 是唯一被证明过的选项，代价是
`-f concat` 永久不可用——而 `concat:` 与 HLS demuxer 都可用，这个代价为零。

A3 是唯一四格全绿的方案，但整行待核验，且 PTS 33-bit 约 26.5 小时回绕、体积估计大 3–6%（未实测）。
不建议换。

**红线：`app/` 下出现 `-f concat` 都是错的。** `step_exporter.py`、`clip_builder.py` 模块 docstring 各记了一遍。

---

## 4. 拼接路径：四条候选

素材：`testsrc` 6.0s / 10fps / 60 帧，切成 3 个 2s fragment + init（827 B）。

```text
#    路径                                       exit   产物            结论
───────────────────────────────────────────────────────────────────────────────
①    HLS demuxer + 落盘 VOD 清单 + 裸文件名 *     0    6.0s / 60 帧    ✅ 最稳
②    HLS demuxer + 清单走 pipe + 绝对 URI   *     0    6.0s / 60 帧    ✅ 可用，约束多
③    concat: 协议（段列表进命令行）               0    6.0s / 60 帧    ✅ 最省
③'   concatf: 协议（段列表进文件）                0    6.0s / 60 帧    ✅ 段数无上限

被排除的写法（每条都有人会去试）：
④    -f concat，清单只列 fragment              183    无              轴 1 判死
⑤    -f concat，清单把 init 也列进去             0    261 B 零流空壳   轴 1 判死，静默（§5.1）
⑥    直接喂 LIVE 清单（无 EXT-X-ENDLIST）       挂死   48 B stub       清单形态错，静默（§5.2）
②'   同 ②，清单里用反斜杠路径                  127    无              平台分歧（见下）
```

\* HLS demuxer 注册在 demuxer 层，但它经 `EXT-X-MAP` 先取 init 再接 fragment，实质走轴 2 的「前置共用头」。

① 与 ③ 的产物只差 18 字节（文件尾的编码器 tag 一类），媒体载荷逐字节相同。

### 四条候选的取舍

| 路径 | 优势 | 局限 | 适合 |
|---|---|---|---|
| ① 落盘 VOD 清单 | 段顺序、init 引用由清单显式声明；能取子集；命令行长度恒定；裸文件名无路径分歧 | 清单须与段同目录（HLS demuxer 按清单所在目录解析）→ 目录须可写；SIGKILL 会残留临时清单；**坏段静默截短**（§5.3） | 段数多、目录可写 |
| ② 清单走 stdin | 不落盘，目录可只读 | URI 全须改绝对，**`#EXT-X-MAP` 漏改时报错指向段而非 init**；须 `-protocol_whitelist file,pipe,crypto,data`；Windows 须正斜杠 | 几乎没有——③ 同样不落盘且约束更少 |
| ③ `concat:` | 零临时文件、目录可只读、正反斜杠都吃、命令最简 | 段列表进命令行：按每条路径 ~80 字符估，Windows `CreateProcess` 32767 字符上限 ≈ 400 段 | 段数数十量级、按帧号定位、目录只读 |
| ③' `concatf:` | ③ 的全部优势 + 段数无上限；列表条目是绝对路径，可放任意目录 | 又有临时文件（位置自由）；**列表里须正斜杠** | 段数上百且不想写产物目录 |

四条都**不能由调用方覆盖段时长**：ffmpeg 的时间轴来自 fragment 的 `tfdt` + sample duration，改写清单
EXTINF 实测是空操作。段时长要在写段时就把 tfdt 打对。

### 路径分隔符只影响清单与列表文件

盘符 `C:` 被当成协议头的担心实测不成立。分歧在斜杠方向：

| 路径写在哪 | 正斜杠 `C:/x/seg.m4s` | 反斜杠 `C:\x\seg.m4s` |
|---|---|---|
| ② HLS 清单里的 URI | ✅ | ❌ exit 127 `No such file or directory` |
| ③ `concat:` 命令行 | ✅ | ✅ |
| ③' `concatf:` 列表文件 | ✅ | ❌ `Invalid data found` |

③ 与 ③' 的不对称是实测结论（2×2 验过正/反斜杠 × LF/CRLF，失败只与斜杠有关）。Windows 上 `str(Path)` 产
反斜杠，往清单或列表文件写绝对路径必须 `as_posix()`；用裸文件名（①）或放命令行（③）可绕开。

### 取子集时 tfdt 不泄漏

| 担心 | 实测（① 与 ③ 一致） |
|---|---|
| 多段拼接时间轴错 | 同帧数同时长，媒体载荷逐字节相同 |
| 输出侧 `-ss/-to` 精度掉 | 6.0s 流截 `[2.5, 4.5]` → 2.0s / 20 帧 |
| fragment 的 tfdt 是整轨绝对位置，取子集会泄漏 | 只取第 2、3 段（整轨 2.0~6.0s）→ `start_time=0` / `duration=4.0`；再截 `[0.5, 1.5]` → 1.0s / 10 帧 |

所以按「拼出来的流从 0 开始」算 seek 偏移成立，这正是 `MediaTimeline.media_offset_ms` 的前提。

### 选型决策

```text
消费者是播放器（hls.js / Safari）？ → HLS 清单 + HTTP URI，本文不适用
消费者是 ffmpeg：
  按帧号精确定位？                  → ③（清单会引入媒体时间轴，帧号要「字节拼起来、n 从 0 数」）
  段数 ≲ 几百？                     → ③
  超过几百：临时文件能落在段目录？  → 能：①    不能：③'（须正斜杠）
```

本仓库链路：

| 链路 | 消费者 | 定位方式 | 段数量级 | 现状 | 评价 |
|---|---|---|---|---|---|
| 离线反查 `storage/hls/_decode.py` | ffmpeg | 帧号（禁 `-ss`） | 1 | ③ | 唯一正确解 |
| 送标裁剪 `lab/clip_builder.py` | ffmpeg | 时间（输出侧 `-ss/-to`） | ≤30（上限 300s） | ① | 维持 ①；换 ③ 只需考虑 §5 判据与 Windows 路径 |
| 整段导出 `lab/step_exporter.py` | ffmpeg | 无 | ~180 | ① | ① 或 ③'（③ 到 ~400 段顶上限） |
| 回放清单 `routers/traceback.py` | 浏览器 | 播放器自理 | ~180 | HLS 清单 | 不适用本文 |

`clip_builder` 与 `step_exporter` 的临时清单都直接用清单 EXTINF 真值；送标区间收媒体刻度，`-ss` 的 seek
基准与 tfdt 同轴。`app/services/utils/vod_playlist.py` 的 `render_vod` 同时服务浏览器（token 化 HTTP URL）
和 ffmpeg（同目录裸文件名），区别只在调用方交进来的 URI。

---

## 5. 验收判据：三种失败骗过 `returncode != 0`

```text
                                 表现             exit   returncode 判据
─────────────────────────────────────────────────────────────────────────────
§5.1 ⑤ -f concat 清单含 init     零流空壳 261 B    0     骗过
§5.2 ⑥ LIVE 清单缺 ENDLIST       无限挂死          —     骗过（只有超时能暴露）
§5.3 ① 坏段 / 未登记段           截短但完全合法     0     骗过，-xerror 也抓不住 ← 最危险
——   ②'/③' 路径用反斜杠          打不开文件        127    抓得到，但只在 Windows
```

现役两处调用点（`step_exporter.py`、`clip_builder.py`）的判据是 `returncode != 0` + 产物存在（`stat()` 不抛），
两处都带超时（`step_exporter` `max(120, 段数×5)`s，`clip_builder` `max(60, 时长×4)`s）。§5.1 走不到（不用 `-f concat`），
§5.2 已被超时兜住（且两处都自拼 VOD 清单），**§5.3 无防护**。

### 5.1 ⑤ 空壳：陷阱在「修复」那一步

④ 报错响亮（exit 183）。看到「找不到 codec init」，最自然的修复是把 `init.mp4` 列进清单第一行，于是变成 ⑤：
exit 0，产物 261 字节（`ftyp + free + 空 mdat + moov`），ffprobe 打得开但流数为 0。

```text
④ 第一个条目 seg0 打不开 → 错误从 open_input 穿出       → exit 183
⑤ 第一个条目 init 打开成功（零帧的合法 MP4）→ 流已声明，后续失败降级成一条日志 → exit 0
```

失败在创建输出文件之前就已发现（`Impossible to open` 早于 `Opening an output file`），ffmpeg 照常往下走；
错误码被重映射成 `AVERROR(EIO) = -5`，真因却是 `INVALIDDATA`——报成磁盘错，排查方向全错。`-xerror` 对 ⑤
有效（实测退出码 127）。

**判据**：`ffprobe` 数流，0 条即空壳。

`clip_builder` 模块 docstring 另记一条同形态失败：`-ss` 挪到 `-i` 之前（输入侧 seek）也 exit 0 产出
261 字节零流空壳。本文未复现，待核验。

### 5.2 ⑥ LIVE 清单：只差 `#EXT-X-ENDLIST` 一行

写侧落盘的 `{track}_playlist.m3u8` 是 LIVE 形态。直接喂给 ffmpeg，它当直播流无限轮询等新段，跑满 120s 超时被杀，
产物 48 字节不可读；加 `-live_start_index 0` 同样挂死。表现是卡住而不是报错，日志为空。

给 ffmpeg 的清单必须是 VOD 形态（`#EXT-X-PLAYLIST-TYPE:VOD` + `#EXT-X-ENDLIST`，即 `render_vod`）。

**判据**：调用侧必须带超时，且超时按失败处理。

### 5.3 ① 遇到坏段或未登记段会静默截短

故障注入：3 段共 6.0s，把中间的 seg1 换成 4000 字节随机数据，走 ①。

```text
条件                  exit   产物     ffprobe duration   stderr
─────────────────────────────────────────────────────────────────
基线（三段完好）        0    ~24 KB      6.000000        无
seg1 损坏，-v error     0     8.8 KB     2.000000        无输出
seg1 损坏，-v warning   0     8.8 KB     2.000000        无输出
seg1 损坏，-xerror      0     8.8 KB     2.000000        无输出
```

产出结构完整、ffprobe 满意、能正常播放，只是少了三分之二。清单里混进未登记的段（在途 / 登记失败）同理，
所以段只能从清单来（[DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md) §5）。

**唯一可行的判据是内容级的：比对输出时长与预期。** 两处调用点手里都有预期值：`step_exporter` 可对
`hls.list_segments` 的 EXTINF 求和，`clip_builder` 有 `ClipResult.duration_ms`。

> **现状缺口：两处都没有这层校验。** `step_exporter` 是 ~180 段 `-c copy` 整段导出，一段坏掉就交付
> 静默截短的成片，服务端日志从头到尾都是成功。修不修、修到哪一档待定。

---

## 6. 复现方法

```bash
FF=.ffmpeg/bin/ffmpeg.exe      # 仓库钉版，实测为 n7.1.4-9

# 素材：3 个 fMP4 fragment + init
"$FF" -f lavfi -i testsrc=size=160x120:rate=10:duration=6 -c:v libx264 -g 10 \
  -f hls -hls_time 2 -hls_segment_type fmp4 -hls_fmp4_init_filename init.mp4 \
  -hls_segment_filename 'seg%d.m4s' -hls_playlist_type vod out.m3u8

# ① / ③ / ③'
"$FF" -allowed_extensions ALL -i out.m3u8 -c copy -y a.mp4
"$FF" -i "concat:init.mp4|seg0.m4s|seg1.m4s|seg2.m4s" -c copy -y b.mp4
printf '%s\n' "$PWD/init.mp4" "$PWD/seg0.m4s" "$PWD/seg1.m4s" > /anywhere/list.txt   # 须正斜杠绝对路径
"$FF" -i "concatf:/anywhere/list.txt" -c copy -y b2.mp4

# ④ exit 183；⑤ exit 0 零流空壳，加 -xerror 变非零
printf "file 'seg0.m4s'\nfile 'seg1.m4s'\n" > list4.txt
"$FF" -f concat -safe 0 -i list4.txt -c copy -y c4.mp4
printf "file 'init.mp4'\nfile 'seg0.m4s'\n" > list5.txt
"$FF" -f concat -safe 0 -i list5.txt -c copy -y c5.mp4
"$FF" -f concat -safe 0 -xerror -i list5.txt -c copy -y c5x.mp4

# §2 的两个 context
"$FF" -loglevel debug -f concat -safe 0 -i list5.txt -c copy -y /dev/null 2>&1 \
  | grep -E "AVFormatContext @|not find corresponding|Impossible|Error during demuxing"

# §5.3 静默截短：中间段换成垃圾，走 ①
cp seg1.m4s seg1.bak && head -c 4000 /dev/urandom > seg1.m4s
"$FF" -v error -allowed_extensions ALL -i out.m3u8 -c copy -y broken.mp4          # exit 0
ffprobe -v error -show_entries format=duration -of default=nw=1 broken.mp4        # 2.0 而非 6.0
cp seg1.bak seg1.m4s

# 验产物：流数为 0 即空壳
ffprobe -v error -show_entries stream=codec_type -of default=nw=1 c5.mp4
```

段数上限按实际路径长度估：`(32767 - 命令其余部分) / (单条路径字符数 + 1)`。
