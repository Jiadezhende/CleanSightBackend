> 更新时间：2026-09-19
> 依据来源：实测（ffmpeg n7.1.4-9，Windows）+ 代码分析
> 可信级别：exit code、产物大小、时长、日志原文均为实测值，复现方法见 §4

# 分段拼接的验收判据：三种失败会骗过 `returncode != 0`

选对拼接命令不等于拿到了正确产物。这里是四个失败点，**三个 exit 0 或静默挂死**，
其中一个**选对路径照样会中**：

```text
                                  表现               exit   returncode+size 判据
──────────────────────────────────────────────────────────────────────────────────
§1  ⑤ -f concat 清单含 init       零流空壳 261 B      0     ☠️ 骗过
§2  ⑥ LIVE 清单缺 ENDLIST         无限挂死            —     ☠️ 骗过（靠超时才暴露）
§3  ① 段内容损坏                   截短但完全合法       0     ☠️ 骗过 ← 最危险
——  ②'/③' 路径用了反斜杠           打不开文件         127    ✅ 抓得到，但只在 Windows
──────────────────────────────────────────────────────────────────────────────────
```

**现役两处调用点用的正是 `returncode != 0` + `st_size > 0` 这对判据**
（`app/services/lab/step_exporter.py`、`app/services/lab/clip_builder.py`），**前三条都抓不住**。

这四条与「怎么选路」正交——选路看两条轴，见
[DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md)；那里也给了 ①②③③' 的编号定义和
路径分隔符那一条（末条已归入该文 §4，因为它决定的是实现怎么写路径，不是验收怎么判）。

---

## 1. ⑤ `-f concat` 清单含 init：陷阱在「修复」那一步

④ 只列 fragment 报错响亮（exit 183、无产物，机制见
[DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md) §2）。看到「找不到 codec init」，最自然的
修复是把 `init.mp4` 列进清单第一行——于是变成 ⑤：exit 0、产物 261 字节
（`ftyp + free + 空 mdat + moov`）、ffprobe 打得开但**流的条数是 0**。

**枢纽是 `init.mp4` 本身就是个零帧的合法 MP4**（`find_stream_info` 结果 `frames:0`）：

```text
④ 第一个条目 seg0 就打不开 → 错误从 open_input 穿出      → exit 183
⑤ 第一个条目 init 打开成功 → 流已声明，失败降级成一条日志 → exit 0
```

两个容易记反的细节：失败**在创建输出文件之前**就已被发现（日志里 `Impossible to open` 早于
`Opening an output file`），ffmpeg 知道了照常往下走；错误码被重映射成 `AVERROR(EIO) = -5`
（"I/O error"），真因却是 `INVALIDDATA`——**报出来是磁盘错，排查方向全是错的**。

> **`-xerror` 对 ⑤ 有效**（退出码变非零，实测 127），但对 §3 无效。

**判据**：`ffprobe` 数流的条数，0 条即空壳。

---

## 2. ⑥ LIVE 清单：区别只有 `#EXT-X-ENDLIST` 一行

边录边写的清单是 LIVE 形态（不写 `ENDLIST`）。复用它看起来最省事，实测 ffmpeg 把它当直播流
无限轮询等新段，跑满 120s 超时被杀，产物 48 字节不可读；加 `-live_start_index 0` 同样挂死。
表现是「卡住」而不是「报错」，日志里什么都没有，归因比 ⑤ 更难。

> **给 ffmpeg 消费的清单必须是 VOD 形态**（`#EXT-X-PLAYLIST-TYPE:VOD` + `#EXT-X-ENDLIST`），
> 与写入侧落盘的 LIVE 清单是两种东西，不能互相替代。

**判据**：调用侧必须带超时，且超时要当失败处理——没有超时这条永远暴露不出来。

---

## 3. ① 遇到坏段会静默截短 —— 最危险的一条

故障注入：3 段共 6.0s 的合法素材，把**中间那段** seg1 换成 4000 字节随机数据，走路径 ①。

```text
条件                     exit   产物     ffprobe duration   stderr
──────────────────────────────────────────────────────────────────
基线（三段完好）           0    ~24 KB      6.000000        无
seg1 损坏，-v error        0     8.8 KB     2.000000        完全无输出
seg1 损坏，-v warning      0     8.8 KB     2.000000        完全无输出
seg1 损坏，-xerror         0     8.8 KB     2.000000        完全无输出
──────────────────────────────────────────────────────────────────
```

**四项全部 exit 0，任何日志级别下 ffmpeg 一个字都不说，`-xerror` 也抓不住。** 产出是结构完整、
ffprobe 满意、能正常播放的 2.0 秒 mp4，只是少了三分之二。比 ⑤ 的空壳更危险——空壳至少零条流、
稍加校验就露馅，这个从任何结构性角度看都是合法产物。

**唯一可行的判据是内容级的：比对输出时长与预期。** 两处调用点手里都已经有预期值——
`step_exporter` 求和 `hls.list_segments` 的逐段 EXTINF，`clip_builder` 用
`end_ms - start_ms`。

> ⚠ **现状：两处都还没有这层校验。** `step_exporter` 是 ~180 段的 `-c copy` 整段导出，用途是
> 汇报素材 / 取原片——一段坏掉就交付一个静默截短的成片，而服务端日志从头到尾是成功。
> 修不修、修到哪一档待定。

---

## 4. 复现方法

素材构造同 [DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md) §6（3 个 fMP4 fragment + init）。

```bash
FF=.ffmpeg/bin/ffmpeg.exe      # 本仓库自带钉版；实测用的是 n7.1.4-9

# §1 静默空壳：-f concat 清单含 init
printf "file 'init.mp4'\nfile 'seg0.m4s'\n" > list5.txt
"$FF" -f concat -safe 0 -i list5.txt -c copy -y c5.mp4             # exit 0，c5.mp4 是空壳
"$FF" -f concat -safe 0 -xerror -i list5.txt -c copy -y c5c.mp4    # 加 -xerror → 非零

# §3 静默截短：把中间那段换成垃圾，走路径 ①
cp seg1.m4s seg1.bak && head -c 4000 /dev/urandom > seg1.m4s
"$FF" -v error -allowed_extensions ALL -i out.m3u8 -c copy -y broken.mp4  # exit 0
ffprobe -v error -show_entries format=duration -of default=nw=1 broken.mp4 # 2.0 而非 6.0
cp seg1.bak seg1.m4s

# 验产物：流的条数为 0 即空壳；时长对不上即截短
ffprobe -v error -show_entries stream=codec_type -of default=nw=1 c5.mp4
```
