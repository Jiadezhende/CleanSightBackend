> 更新时间：2026-09-19
> 依据来源：实测（ffmpeg n7.1.4-9，Windows）+ 代码分析
> 可信级别：exit code、产物大小、时长、帧数、日志原文均为实测值，复现方法见 §6；
> 标注「待核验」的是未在本仓库验证过的推导，不作为事实采信

# 分段视频落盘容器与拼接方式选型：两条正交的轴定完一切

盘上是 `init.mp4` + N 个 fragment，要把它们变成一条连续流交给 ffmpeg（导出、裁剪、解帧）。
**选型只有两个问题，答案都由同两条轴推出**：

```text
落盘容器怎么选？   §3   回放端是 MSE，A1 普通 MP4 出局，B1「fragment + 共用 init」是唯一被证过的选项。
拼接方式怎么选？   §4   B1 下八种写法里只有四条是真候选，`-f concat` 全家结构上不可能。
```

两条轴是：**段能否被单独打开**（决定 `-f concat` 能不能用）、**段能否按字节拼起来**
（决定 `concat:` / `concatf:` 能不能用）。§2 先把轴讲清楚，§3 §4 是它的两个推论。

本文只管**选哪条路**。命令跑完之后怎么确认拿到的是正确产物（有三种失败会骗过
`returncode != 0` 型判据），是与两条轴正交的另一码事，见
[DESIGN_SEGMENT_CONCAT_VERIFY.md](DESIGN_SEGMENT_CONCAT_VERIFY.md)。
「清单给浏览器播放」那一类场景属 HLS 协议本身，见
[DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)。

---

## 1. 问题的形状：codec 参数不在段自己身上

```text
init.mp4     ftyp + moov               ← 有头、没画面（SPS/PPS、timescale、trex 在这里）
seg_*.m4s    styp + sidx + moof/mdat   ← 有画面、没头
```

这是 fMP4 的设计：整轨共用一份 `moov`，每个 fragment 只带自己的 `moof`+`mdat`。
**单独打开任何一个 fragment 都解不出它是什么编码**——这一条是下面两条轴的起点。

---

## 2. 两条正交的轴

```text
轴 1  段能否被单独 demux 出完整 codec 参数？   → 决定 `-f concat` demuxer 能不能用
轴 2  段能否被字节拼接成一个整体？              → 决定 `concat:` / `concatf:` 协议能不能用
                                                （允许前置一份共用头）
```

两条轴判的是**不同的东西**，所以会出现「一个能用另一个不能用」的交叉：

```text
格式                     轴 1 独立 demux        轴 2 字节可拼
──────────────────────────────────────────────────────────────────────────
普通 MP4                    ✅                    ❌  多份 moov 拼一起是垃圾
自含式 fMP4 / CMAF          ✅                    ❌  同上
MPEG-TS                     ✅                    ✅  无唯一全局头
fMP4 fragment + init        ❌                    ✅  前置 init 即可      ← 本仓库
```

### 轴 2 对 fragment 成立的原因很特殊

fragment 本身不自解释，但它缺的那份头**可以被前置**——`concat:init|seg` 拼出来的字节流在上层
看来就是一个完整 mp4，probe 结果是 `mov,mp4`，`moov` 与 `moof` 在同一个轨道表里相遇。

普通 MP4 不行不是因为缺头，是因为头**有且只能有一个**，第二份就是污染。

一句话：**`-f concat` 要的是「每个条目自解释」，`concat:` 要的是「拼起来之后整体自解释」。**

### 轴 1 为什么把 `-f concat` 判死：每个条目一个独立 context

`-f concat` 对清单里每个条目**开一个全新的 `AVFormatContext`**，用那个文件自己的 demuxer 读包。
`-loglevel debug` 直接把指针打出来了（清单为 `init.mp4` + `seg0.m4s`）：

```text
[AVFormatContext @ ...755b40] Opening 'init.mp4' for reading          ← context A
[mov,mp4 @ ...755b40] After avformat_find_stream_info() ... frames:0
[AVFormatContext @ ...764880] Opening 'seg0.m4s' for reading          ← context B，另一个指针
[mov,mp4 @ ...764880] could not find corresponding track id 1
[mov,mp4 @ ...764880] could not find corresponding trex (id 1)
[mov,mp4 @ ...764880] could not find corresponding track id 0
[mov,mp4 @ ...764880] trun track id unknown, no tfhd was found
[mov,mp4 @ ...764880] error reading header
[concat @ ...f4900]   Impossible to open 'seg0.m4s'
```

失败链条逐级是：

```text
seg0.m4s = styp + sidx + moof + mdat
   ├─ moof/traf/tfhd 声明 track_ID = 1
   │     → 在 context B 的轨道表里查 1 → 表是空的（没有 moov/trak）
   ├─ 默认 sample 值本该来自 moov/mvex/trex
   │     → 同样不在 context B 里
   └─ tfhd 被丢弃后 trun 没有宿主 → error reading header → INVALIDDATA
```

**`moov` 里的三样东西（`trak` 轨道表、`stsd/avcC` 的 SPS/PPS、`mvex/trex` 的默认 sample 值）
全部随 context A 一起作废。** concat demuxer 从头到尾没有「把前一个条目的头传给后一个」这种机制
——所以**把 `init.mp4` 列进清单第一行也没用**，那只是多开一个 context。

### ⚠ 有两个都叫 `concat` 的东西，注册在不同的层

这是全部混淆的来源。ffmpeg 自己列得很清楚：

```bash
$ ffmpeg -protocols | grep concat
concat    concatf                          ← 协议，两个

$ ffmpeg -demuxers | grep concat
 D   concat    Virtual concatenation script  ← demuxer，一个
```

- **协议 `concat:` / `concatf:`**：实现的是「读字节」，把多个子 URL 的字节流首尾相接，
  对上层完全透明——上层 probe 出 `mov,mp4`，以为自己在读一个普通 mp4 文件。**吃轴 2。**
- **demuxer `concat`**：**它不是媒体容器**。ffmpeg 给它的描述是「Virtual concatenation script」
  ——它解析的是一份**纯文本清单**，该文本格式叫 `ffconcat`，有自己的魔数 `ffconcat version 1.0`。
  它是**调度器**，对每个条目递归开一个完整输入上下文。**吃轴 1。**

ffmpeg 输入分三层，两个 concat 各在一层：

```text
协议层   file: / pipe: / data: / concat: / concatf: / http:    给字节        ← 轴 2
demuxer 层   mov,mp4 / hls / concat                            把字节解成包   ← 轴 1
解码层   h264 / hevc / ...                                     把包解成帧
```

---

## 3. 落盘容器选型：分界线是 MSE，所以没有自由度

### A 类：段自包含（轴 1 ✅）

```text
A1  普通 MP4          ftyp + moov + mdat             moov 在段内
A2  自含式 fMP4/CMAF  ftyp + moov + moof + mdat      每段自带一份 init（实测 init 为 827 B）
A3  MPEG-TS           无 moov                         PAT/PMT + 带内 SPS/PPS 周期重复
```

A3 是这类里的异类：它自包含**不是**靠 moov，是靠头信息周期性重复——这也是它同时满足轴 2 的原因。

### B 类：段是 fragment + 共用 init（轴 1 ❌、轴 2 ✅）

```text
B1  fMP4 fragment + {track}_init.mp4    styp + sidx + moof + mdat    ← 现役
```

### 消费端矩阵

```text
                        hls.js / MSE       concat: 协议    -f concat      ① HLS 清单
──────────────────────────────────────────────────────────────────────────────────────
A1  普通 MP4            ❌ 实测记录         ❌ 待核验        ✅ 待核验       ? 待核验
A2  自含式 CMAF         ? 待核验            ❌ 待核验        ✅ 待核验       ? 待核验
A3  MPEG-TS             ✅ 客户端转封装      ✅ 待核验        ✅ 待核验       ✅ 待核验
──────────────────────────────────────────────────────────────────────────────────────
B1  fragment + init     ✅ 实测             ✅ 实测          ❌ 实测         ✅ 实测
```

A1 的 ❌（MSE）来源：`app/storage/hls/_fmp4.py` 模块 docstring——普通 MP4（moov+mdat 整体）
被 hls.js 当段播会 `fragParsingError`。B1 一行四格全部实测，A 类除 A1 的 MSE 一格外整片待核验。

### 结论：B1，代价是零

回放端是 hls.js / 原生 MSE，**A1 直接出局**（实测记录），A2 未验证且要为每段多付一份 moov。
B1 是唯一被证明过的选项，代价是 `-f concat` 永久不可用——而这个代价是零，因为 `concat:` 协议
与路径 ① 都可用且已在生产链路上跑。

> **A3（MPEG-TS）是矩阵里唯一四格全绿的方案**，理论上能让「随便用哪条路径都不掉坑」。但整行
> 都是待核验推导，且带两个已知代价：PTS 33-bit 在 ~26.5 小时回绕、体积大于 fMP4（估 3–6%，
> 未实测）。**不建议动**——B1 的坑已经全部买单完毕，换 A3 是拿「已知已解决」换「未知待验证」。

### 落到红线

**`app/` 下任何地方出现 `-f concat` 都是错的，没有例外**（理由见 §2，本文不再重复）。现有三处
注释各写了一遍：`app/routers/lab.py`、`app/services/lab/step_exporter.py`、
`app/services/lab/clip_builder.py` 的模块 docstring。

---

## 4. 拼接方式选型：B1 下的候选只有四条

素材：`testsrc` 6.0s / 10fps / 60 帧，切成 3 个 2 秒 fMP4 fragment + 1 个 init（827 B）。

**`-f concat` 全家（④⑤）不在候选之列**（§2）。真正要在其中选的是这四条，全部走轴 2：

```text
#    路径                                         exit   产物             结论
────────────────────────────────────────────────────────────────────────────────
①    HLS demuxer + 落盘 VOD 清单 + 裸文件名  *     0     6.0s / 60 帧     ✅ 最稳
②    HLS demuxer + 清单走 pipe + 绝对 URI    *     0     6.0s / 60 帧     ✅ 可用，约束多
③    `concat:` 协议（段列表进命令行）               0     6.0s / 60 帧     ✅ 最省
③'   `concatf:` 协议（段列表进文件）                0     6.0s / 60 帧     ✅ 段数无上限
────────────────────────────────────────────────────────────────────────────────
```

\* HLS demuxer 注册在容器层，但它通过 `EXT-X-MAP` 先取 init 再接 fragment，实质走的是轴 2 的
「前置共用头」路线，所以能吃 B1。

① 与 ③ 的产物**只差 18 个字节**，全在文件尾部的容器元数据区（编码器 tag 一类），
媒体载荷逐字节相同。

### 四条被排除的写法，以及它们实测长什么样

它们仍然记在这里，因为**每一条都有人会去试**——尤其 ④→⑤ 是一条极其自然的「修复」路径。

```text
#    写法                                         exit   产物             为什么排除
────────────────────────────────────────────────────────────────────────────────
④    `-f concat`，清单只列 fragment              183    无               轴 1 判死（§2）
⑤    `-f concat`，清单把 init 也列进去             0    261 B、零条流     轴 1 判死（§2）
⑥    直接喂 LIVE 清单（无 `EXT-X-ENDLIST`）      挂死    48 B stub        清单形态错，不是轴的事
②'   同 ②，但清单里用反斜杠路径                    127    无               平台分歧，见下方斜杠一节
────────────────────────────────────────────────────────────────────────────────
```

④⑤ 同因同源，差别只在响亮还是静默：⑤ 的 `init.mp4` 本身是个零帧的合法 MP4，第一个条目打开成功
→ 流已声明 → 后续失败降级成一条日志 → exit 0 拿到空壳。**⑤ 与 ⑥ 会骗过 `returncode != 0` 型
判据**，机制与判据怎么写见
[DESIGN_SEGMENT_CONCAT_VERIFY.md](DESIGN_SEGMENT_CONCAT_VERIFY.md) §1 §2。

### 四条候选的优势与局限

#### ① HLS demuxer + 落盘 VOD 清单（裸文件名）

```bash
ffmpeg -allowed_extensions ALL -i /path/to/step/hls/.tmp_xxx.m3u8 -c copy out.mp4
```

清单里 `#EXT-X-MAP:URI="init.mp4"` 与各段都写**裸文件名**，HLS demuxer 按**清单自身所在目录**
解析——所以清单必须和段放在同一个目录。

| | |
|---|---|
| **优势** | 段顺序、每段时长（EXTINF）、init 引用都由清单显式声明，ffmpeg 不用猜；能表达「只取子集」；能**覆盖**每段时长（下游按清单的 EXTINF 建时间轴，见 §5 的待核验项）；命令行长度恒定，段数无上限；跨平台无路径分歧（裸文件名） |
| **局限** | 必须落一个临时文件到段所在目录 → 该目录必须可写（只读挂载不可用）；进程被 SIGKILL 时残留清单；临时文件是「非本域产物」，与按域隔离的落盘结构有张力；**坏段会静默截短**（VERIFY §3） |
| **适合** | 段数多（数百）、目录可写、需要显式声明每段时长的场景 |

#### ② HLS demuxer + 清单走 stdin

```bash
cat playlist.m3u8 | ffmpeg -allowed_extensions ALL \
  -protocol_whitelist file,pipe,crypto,data -f hls -i pipe:0 -c copy out.mp4
```

省掉临时文件，但代价是清单里的 URI 必须全部改成**绝对路径**（管道没有 base 目录）。

| | |
|---|---|
| **优势** | 不落盘，目录可只读；无残留 |
| **局限** | ① `#EXT-X-MAP:URI` **也**必须改绝对，漏改的报错是 `Error when loading first segment`，指向段而不是 init，排查方向完全是错的；② 必须显式给 `-protocol_whitelist`；③ **Windows 上必须用正斜杠**，见下方斜杠一节 |
| **适合** | 几乎没有——③ 在同样「不落盘」的前提下约束更少。列在这里是为了说明它可行但不划算 |

#### ③ `concat:` 协议

```bash
ffmpeg -i "concat:init.mp4|seg0.m4s|seg1.m4s|seg2.m4s" -c copy out.mp4
```

| | |
|---|---|
| **优势** | 零临时文件、零残留、目录可只读；跨平台路径无分歧（**正反斜杠都吃**）；命令形态最简单 |
| **局限** | **段列表进命令行 → 有长度上限**。按每条路径 ~80 字符估，Windows `CreateProcess` 上限 32767 字符 ≈ 400 段；Linux `ARG_MAX` 宽得多。超限时改用 ③'。段时长由 fragment 自身的 `tfdt` + sample duration 推导，**无法像 EXTINF 那样由调用方显式指定** |
| **适合** | 段数可控（数十量级）、或需要按帧号精确定位、或目录不可写的场景 |

#### ③' `concatf:` 协议（段列表放文件）

```bash
printf 'C:/x/init.mp4\nC:/x/seg0.m4s\nC:/x/seg1.m4s\n' > /anywhere/list.txt
ffmpeg -i "concatf:/anywhere/list.txt" -c copy out.mp4
```

| | |
|---|---|
| **优势** | ③ 的全部优势，**外加段数无上限**（列表不进命令行）。列表文件**可以放在任意目录**——条目是绝对路径，不像 HLS 清单必须和段同目录，所以它能落在真正的临时目录里，不污染产物目录 |
| **局限** | 又有了一个临时文件（但位置自由）；**列表里必须用正斜杠**，反斜杠失败——注意这与 ③ 不同，见下方斜杠一节 |
| **适合** | 段数上百、且不希望往产物目录里写东西的场景。它是 ① 与 ③ 的折中 |

### 路径分隔符：影响清单与列表文件，不影响命令行

常见的担心是「盘符 `C:` 会被当成协议头解析」——**实测没有发生**。真正的分歧在斜杠方向，
它决定了 ② / ③' 的实现必须怎么写路径：

| 路径写在哪 | 正斜杠 `C:/x/seg.m4s` | 反斜杠 `C:\x\seg.m4s` |
|---|---|---|
| ② HLS demuxer（清单里的 URI） | ✅ | ❌ `exit 127: No such file or directory` |
| ③ `concat:` 协议（命令行） | ✅ | ✅ |
| ③' `concatf:` 协议（列表文件） | ✅ | ❌ `Invalid data found` |

**③ 与 ③' 的不对称是实测结论，不是笔误**：同一个协议，路径写在命令行里容忍反斜杠，
写进列表文件就不行。用 2×2 验过（正/反斜杠 × LF/CRLF），失败只跟斜杠方向相关，与行尾无关。

**这条在 Windows 上炸、在 Linux 上没事**：Python 的 `str(Path)` 在 Windows 天然产反斜杠，
`Path.as_posix()` 产正斜杠。任何往清单或列表文件里写绝对路径的实现都必须显式 `as_posix()`；
用裸文件名（①）或把路径放命令行（③）可以绕开。

### 关于 ③ / ③' 的三个常见担心，实测都不成立

| 担心 | 实测 |
|---|---|
| 多段拼接时间轴会错 | 与 ① 同帧数同时长，媒体载荷逐字节相同 |
| 输出侧 `-ss/-to` 精度会掉 | 在 6.0s 流上截 `[2.5, 4.5]`，① 与 ③ 都是 2.0s / 20 帧 |
| **取子集时 `tfdt` 会泄漏**（fragment 的 tfdt 是相对整轨的绝对位置，不是相对子集） | **不泄漏**。只取第 2、3 段（整轨上的 2.0~6.0s），两条路径都归一到 `start_time=0` / `duration=4.0`；在其上再截 `[0.5, 1.5]` 也都是 1.0s / 10 帧 |

第三条最值得记：调用方若按「拼出来的流从 0 开始」计算 seek 偏移（例如
`offset = 目标墙钟 - 首段起始墙钟`），这个假设在两条路径下都成立。

---

## 5. 选型决策

```text
消费者是播放器（hls.js / Safari）？
  └─ 是 → 只能是 HLS 清单，且 URI 是 HTTP。本文其余部分不适用
  └─ 否（消费者是 ffmpeg）↓

需要按「帧号」精确定位（而不是按时间）？
  └─ 是 → ③ concat: 协议。清单会引入媒体时间轴，而帧号要的是「字节拼起来、n 从 0 数」
  └─ 否 ↓

需要由调用方**覆盖**每段时长（不用段自带的 tfdt 口径）？
  └─ 是 → 只能 ①（③/③' 给不出这个能力）。先读下方待核验项
  └─ 否 ↓

段数会超过几百？
  └─ 否 → ③ concat: 协议（零临时文件，路径可正可反斜杠）
  └─ 是 ↓
       临时文件能落在段所在目录吗？
         └─ 能 → ① 落盘 VOD 清单
         └─ 不能（只读挂载 / 不想污染产物目录）→ ③' concatf:（列表可放任意位置，须正斜杠）
```

本仓库四条链路的对应：

| 链路 | 消费者 | 要子集 | 定位方式 | 段数量级 | 现状 | 适配 |
|---|---|---|---|---|---|---|
| 离线反查 [`storage/hls/_decode.py`](../../app/storage/hls/_decode.py) | ffmpeg | 单段 | **帧号**（禁用 `-ss`） | 1 | ③ | ✅ 唯一正确解 |
| 送标裁剪 [`lab/clip_builder.py`](../../app/services/lab/clip_builder.py) | ffmpeg | 是 | 时间 | ≤30 | ① | **维持 ①**，见下 |
| 整段导出 [`lab/step_exporter.py`](../../app/services/lab/step_exporter.py) | ffmpeg | 否 | 无 | ~180 | ① | ① 或 ③'（③ 到 ~400 段顶到上限） |
| 回放清单 [`routers/traceback.py`](../../app/routers/traceback.py) | **浏览器** | 否 | 播放器自理 | ~180 | HLS 清单 | ✅ 不适用本文 |

> **待核验：`clip_builder` 能不能换 ③。** 它写临时清单时**刻意不用盘上的 EXTINF**，改用相邻段
> ts 差（墙钟实测值），因为 `-ss` 的 seek 基准是墙钟；而盘上 fragment 的 tfdt 被 hex-patch 成的是
> 「累计 EXTINF × 90000」，EXTINF = `帧数/fps`（媒体时长）。**两套时长口径在盘上不一致。**
> 于是问题是：HLS demuxer 解 fMP4 段时，包的时间戳听清单的 EXTINF 还是听 fragment 自己的 tfdt？
>
> - 听 EXTINF → 路径 ① 是**承重的**，那份临时清单是唯一能注入墙钟时长的地方，换 ③ 会让 seek
>   基准静默退回媒体时间轴，表现是长任务上裁剪位置逐渐漂移。
> - 听 tfdt → `clip_builder` 现在写的墙钟 EXTINF 就是装饰，它的 seek 基准**已经**是错的。
>
> **§6 的复现材料区分不了这两种**：那里用的是 ffmpeg 原生产出的 fragment，EXTINF 与 tfdt 天然
> 一致，两种解释给出同一个结果。要判别得另造一份两者故意错开的材料（如 EXTINF 写 12s、
> tfdt 按 10s 打）。**在此之前不要按「③ 更省」去切 `clip_builder`。**
> `step_exporter` 不受影响（它用 `list_segments` 的 EXTINF 真值，与 tfdt 同源）。
>
> ---
>
> [`services/utils/vod_playlist.py`](../../app/services/utils/vod_playlist.py) 的 `render_vod`
> **同时服务两类消费者**（浏览器与 ffmpeg），容易被读成「一份清单一套用法」。区别在 URI：
> 给浏览器的是 token 化 HTTP URL，给 ffmpeg 的是同目录裸文件名。

---

## 6. 复现方法

```bash
FF=.ffmpeg/bin/ffmpeg.exe      # 本仓库自带钉版；实测用的是 n7.1.4-9

# 造素材：3 个 fMP4 fragment + init
"$FF" -f lavfi -i testsrc=size=160x120:rate=10:duration=6 -c:v libx264 -g 10 \
  -f hls -hls_time 2 -hls_segment_type fmp4 -hls_fmp4_init_filename init.mp4 \
  -hls_segment_filename 'seg%d.m4s' -hls_playlist_type vod out.m3u8

# ① 落盘清单
"$FF" -allowed_extensions ALL -i out.m3u8 -c copy -y a.mp4

# ③ concat 协议（列表进命令行）
"$FF" -i "concat:init.mp4|seg0.m4s|seg1.m4s|seg2.m4s" -c copy -y b.mp4

# ③' concatf 协议（列表进文件，可放任意目录，条目须正斜杠绝对路径）
printf '%s\n' "$PWD/init.mp4" "$PWD/seg0.m4s" "$PWD/seg1.m4s" > /anywhere/list.txt
"$FF" -i "concatf:/anywhere/list.txt" -c copy -y b2.mp4

# ④ 被排除写法：-f concat 清单只列 fragment
printf "file 'seg0.m4s'\nfile 'seg1.m4s'\n" > list4.txt
"$FF" -f concat -safe 0 -i list4.txt -c copy -y c4.mp4     # exit 183，无产物

# ⑤ 被排除写法：-f concat 清单含 init
printf "file 'init.mp4'\nfile 'seg0.m4s'\n" > list5.txt
"$FF" -f concat -safe 0 -i list5.txt -c copy -y c5.mp4     # exit 0，c5.mp4 是零流空壳

# §2 的两个 context：看 AVFormatContext 指针不同
"$FF" -loglevel debug -f concat -safe 0 -i list5.txt -c copy -y /dev/null 2>&1 \
  | grep -E "AVFormatContext @|not find corresponding|Impossible|Error during demuxing"
```

段数上限按实际路径长度估：`(32767 - 命令其余部分) / (单条路径字符数 + 1)`。

失败形态的复现（⑤ 空壳、⑥ 挂死、坏段静默截短）在
[DESIGN_SEGMENT_CONCAT_VERIFY.md](DESIGN_SEGMENT_CONCAT_VERIFY.md) §4，素材同上。
