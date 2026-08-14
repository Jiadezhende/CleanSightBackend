# ClipBuilder 裁剪改两步：先 copy 出裸 H.264，再按 target_fps 精确裁剪

> **变更状态**：生效中（2026-08-14）——**⚠ 临时止血，有明确退出条件，见文末「退出条件」**
> **知识库**：**不单独沉淀**。本改造预期在写入侧时基修复后撤销，沉淀会把一个过渡态写成常态。
>
> 承接：直接缓解 [20260614_LAB_CLIP_TIME_MODEL.md](20260614_LAB_CLIP_TIME_MODEL.md)「已知/延后项 #2 裁剪偏移漂移」——`offset` 混用墙钟与媒体时间，使 `-ss/-to` 裁剪起点偏移。
>
> **根因不在本模块**：见 [20260813_HLS_SEGMENT_TIMESCALE_FIX.md](20260813_HLS_SEGMENT_TIMESCALE_FIX.md)。本文只治 ClipBuilder 这一条出口，写入侧未动，其余出口（在线回放、告警证据回放、StepExporter 整段下载）仍坏。

## 概述

- **改了什么**：[`ClipBuilder._run_ffmpeg`](../../app/services/lab/clip_builder.py) 由单条 ffmpeg 命令拆成两条——① HLS demux + `h264_mp4toannexb` 把 fMP4 fragment 拷成裸 Annex B H.264（`-c:v copy` 不重编码）；② 在裸流上以 `-r target_fps` 显式重定时 PTS，再做 `-ss/-to` 精确裁剪 + libx264 重编码。`target_fps` **直接取自 `settings.raw_fps`**（方法体内 `from app.settings import settings; target_fps = settings.raw_fps`），绑定跨模块单一真源，而非方法形参或硬编码默认值。
- **为什么改**：段落盘按逐段反推的 `eff_fps` 编码（`eff_fps = (N-1)/span`，[`_effective_fps`](../../app/services/persistence/strategies/hls_strategy.py)），raw/processed 各段 eff_fps 随采集漂移而不同（processed ~11-15fps、raw 名义 30 实际可漂移）——**保存视频片的帧率本身就不一致**。原方案单步 `ffmpeg -i tmp_m3u8 -ss/-to -c:v libx264` 直接对 HLS 拼接流重编码，demuxer 按 m3u8 EXTINF 重建 PTS，而各段 eff_fps 不同 ⟹ fragment 实际媒体时长 `N/eff_fps` 各段不一、且与 ClipBuilder 临时 m3u8 的 EXTINF（相邻段 `ts_us` 跨度）仅近似相等，demuxer 重建的 PTS 在段衔接处跳变、与 fragment 内部帧率也不自洽。直接重编码继承这套混乱 PTS，**表现为导出视频播放忽快忽慢、甚至中断播放**。拆两步把帧率基准显式化：cmd1 `-c:v copy` 剥离容器 PTS 得到裸流，cmd2 用单一 `-r target_fps` 为整条裸流统一重分配 PTS（帧序 / target_fps），消除段间基准跳变，`-ss/-to` 裁剪定位可控可调。
- **影响面**：仅 [`_run_ffmpeg`](../../app/services/lab/clip_builder.py)。新增一个中间临时文件 `.clip_{nonce}.h264`；`finally` 块新增一行清理。对外行为（`build_one` / `build_all` 契约、产物路径、异常类型）不变。

## 改动详情

### 1. 新增中间裸流文件

[`clip_builder.py:322`](../../app/services/lab/clip_builder.py#L322)：
```python
tmp_h264 = step_dir / f".clip_{nonce}.h264"
```
落在 step 目录，与 `tmp_m3u8` 同目录（m3u8 的相对 URI 也能解析到 init/段）。

### 2. `target_fps` 绑定 `settings.raw_fps`

[`clip_builder.py:344-345`](../../app/services/lab/clip_builder.py#L344-L345)：
```python
from app.settings import settings
target_fps = settings.raw_fps
```

[`settings.raw_fps`](../../app/settings.py#L93) 是视频/推理/CA 子系统的单一真源（[20260723_CONFIG_LAYERING_FPS_TIME.md](20260723_CONFIG_LAYERING_FPS_TIME.md)），四方共读：Decoder `default_fps`、HLS raw fallback `_effective_fps` 兜底、CA 秒→帧数换算、以及本次新增的 ClipBuilder 重定时。**输入侧（解码→切段）与输出侧（ClipBuilder 重编码）速率基准一致**。

### 3. 单条 ffmpeg 拆成两条

#### 旧（单步：HLS demux 直接 -ss/-to 重编码）
```python
cmd = [
    self._ffmpeg,
    "-y", "-loglevel", "error",
    "-allowed_extensions", "ALL",
    "-i", str(tmp_m3u8),
    "-ss", f"{offset_s:.3f}",
    "-to", f"{end_s:.3f}",
    "-c:v", "libx264",
    "-preset", self._preset,
    "-crf", "23",
    "-pix_fmt", "yuv420p",
    "-an",
    "-movflags", "+faststart",
    "-f", "mp4",
    str(output_path),
]
```

#### 新（两步）
```python
# cmd1：HLS demux → copy + h264_mp4toannexb → 裸 Annex B
cmd1 = [
    self._ffmpeg,
    "-y", "-loglevel", "error",
    "-allowed_extensions", "ALL",
    "-i", str(tmp_m3u8),
    "-c:v", "copy",
    "-bsf", "h264_mp4toannexb",
    str(tmp_h264),
]

# cmd2：裸流按 settings.raw_fps 统一重定时 → -ss/-to 精确裁剪 + libx264
cmd2 = [
    self._ffmpeg,
    "-y", "-loglevel", "error",
    "-r", str(target_fps),       # 输入选项；target_fps = settings.raw_fps（四方单一真源）
    "-i", str(tmp_h264),
    "-ss", f"{offset_s:.3f}",
    "-to", f"{end_s:.3f}",
    "-c:v", "libx264",
    "-preset", self._preset,
    "-crf", "23",
    "-pix_fmt", "yuv420p",
    "-an",
    "-movflags", "+faststart",
    "-f", "mp4",
    str(output_path),
]
```

关键约定：

> **cmd1 用 `-c:v copy`**：直接重编码会继承 demuxer 按（各段不一的）EXTINF 重建的混乱 PTS，输出 mp4 播放忽快忽慢/中断。`copy` + `h264_mp4toannexb` 只搬编码数据、不碰 PTS，拿到"纯编码数据、无容器时戳"的裸流，为 cmd2 统一重定时留干净起点。像素级无损，CPU 近零。

> **`-r target_fps` 必须在 `-i` 前**（输入选项）：作用是"为无时戳裸流指定帧率、重算 PTS = 帧序号 / target_fps"；放 `-i` 后会变成输出帧率重采样（丢帧/重复帧），语义错误。

> **两步而非单步**：各段 eff_fps 不一致是时间轴混乱的根因。cmd1 剥离所有容器 PTS → cmd2 用单一 `target_fps = settings.raw_fps` 统一重分配 PTS，把"段间基准不一"降为"统一速率"。`target_fps` 不要求等于每段 eff_fps，只是稳定口径；绑定 settings.raw_fps 既保证出入速率一致，又让运维改 `CLEANSIGHT_RAW_FPS` 时自动同步。

### 4. 两步分别检查返回码 + `finally` 清理两个临时文件

cmd1 失败时立即抛 `ClipBuildError`（截断 stderr 末 1500 字符），不再进入 cmd2。`finally` 块新增一行 `tmp_h264.unlink(missing_ok=True)`（[clip_builder.py:401](../../app/services/lab/clip_builder.py#L401)）。

## 保留项（不改动）

- **raw-only**：ClipBuilder 仍只吃 raw 轨，不接 processed。
- **libx264 重编码**：ms 精度裁剪必须 `-ss/-to`，与 [`StepExporter`](../../app/services/lab/step_exporter.py) 整段 `-c copy` remux 是两种性质，不合并、不共用参数。
- **EXTINF 用相邻段 ts 跨度**：m3u8 里每段 EXTINF 仍按实测墙钟跨度，末段用中位估算兜底。
- **`_validate_continuity`**：连续性判据仍按 step 实测节奏中位数。

## 验证

| 项 | 结果 |
|----|------|
| `tests/test_lab_clip_builder.py` | **2 failed, 10 passed** |
| `tests/test_lab_step_exporter.py` | 13 passed（不受影响） |
| `tests/test_lab_tasks_api.py` | 3 passed（不受影响） |
| 全量 `pytest tests/` | 未跑（待 clip_builder 失败项处理后再跑） |

### 未通过测试说明

两个失败均因测试假设 `_run_ffmpeg` 只调用一次 `subprocess.run`（用 `captured["cmd"]` 覆盖记录最后一次），而本次改动拆成 cmd1+cmd2 两次调用，`captured["cmd"]` 被 cmd2 覆盖：

1. **`TestRunFfmpegM3u8::test_writes_m3u8_with_init_map_and_segment_list`** — 测试在 `fake_run` 中对每次调用都要求 `cmd` 含 `.m3u8` 输入，但 cmd2 输入是 `.h264`，断言 `m3u8 is not None` 失败：`AssertionError: ffmpeg 调用时临时 m3u8 必须存在`。
2. **`TestRunFfmpegM3u8::test_cmd_uses_hls_demuxer_not_concat`** — 测试从最后一次 `cmd`（cmd2）断言 `-allowed_extensions`，但 `-allowed_extensions` 只在 cmd1，断言失败：`AssertionError: assert '-allowed_extensions' in ['.ffmpeg/bin/ffmpeg', '-y', '-loglevel', 'error', '-r', '30', ...]`。

> 已按 ① 处理：`fake_run` 单槽 `captured["cmd"]` 换成 `_FfmpegCalls` 记录器（收集全部调用，`copy_cmd` / `encode_cmd` 显式指名取哪一步）。单槽写法在两步管线下会被第二次调用覆盖，使「输入是 m3u8」「含 `-allowed_extensions`」这类只对 copy 步成立的断言静默错位到 encode 步——这正是两个用例失败的机制。另加 `test_two_pass_structure` 锁住两步结构本身，并让清理用例先落下部分裸流再失败（否则「h264 被清理」是平凡成立的假绿）。**13 passed**。

---

## 退出条件

**本改造是绕过写入侧时基 bug 的止血，不是根因修复。**

根因：段落盘时每段的 `mdhd.timescale` 各不相同，却共用同一份 `init.mp4`，于是 HLS demuxer 一律拿 init 的 timescale 去解每一段 → 逐段速率 0.63×~1.59× 乱跳。实测与诊断见 [20260813_HLS_SEGMENT_TIMESCALE_FIX.md](20260813_HLS_SEGMENT_TIMESCALE_FIX.md)。本文的两步管线之所以奏效，是因为 cmd1 把被污染的 PTS 整个丢弃、cmd2 用单一 `target_fps` 重建了一条时间轴——**问题被压平，不是被解决**。

因此，上述时基修复落地后**必须撤销本两步改造、恢复单步 HLS demux**：

1. 那时 demux 出的 PTS 已经正确，再重定时反而把正确的墙钟轴抹平成帧计数轴；
2. 与后续「空洞物化为低帧率黑屏段」**直接冲突**——1fps × 30 帧（代表 30s 断流）经 `-r 30` 重定时会被播成 1s，丢失 29s。见 [20260813_HLS_WALLCLOCK_TIMELINE_REQUIREMENTS.md](20260813_HLS_WALLCLOCK_TIMELINE_REQUIREMENTS.md)。

撤销时的信号：`tests/test_lab_clip_builder.py::test_two_pass_structure` 会失败。它失败即提示"两步结构没了"——那时应确认是有意撤销，随之删除该用例。

### 遗留边界（本期不处理）

- `-r settings.raw_fps` 成立的前提是**本管线只吃 raw 轨**（实测真实帧率 29.95 ≈ 30）。processed 轨实测 ~14.7fps，若将来把 `list_segments` 的 track 改成 processed 而不同步改重定时基准，会**静默快放 2.04×**，无任何断言拦截。
- 重定时用的是帧计数轴而非墙钟轴，故段间空洞会被压掉。当前 raw 轨全 13 段核算误差 −0.03%（3776 帧 / 30fps = 125.867s vs ΣEXTINF 125.904s），可忽略；但这依赖"raw 真实帧率恰好 ≈ raw_fps"这一数值巧合，掉速时会失准。
