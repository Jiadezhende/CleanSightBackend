# 录像段改为帧经 stdin 单次编码：去掉 mp4v 中间文件，单段落盘提速约 3.8 倍

> **变更状态**：生效中（2026-10-13）
> **知识库**：待沉淀

## 概述

`insert_segment` 第 ① 步，从「cv2 写 mp4v 中间文件 → ffmpeg 转码成 fMP4」改为「帧以 rawvideo 经 stdin 灌给 ffmpeg，一次编码成 fMP4」（新增 [`_fmp4.encode`](../../app/storage/hls/_fmp4.py)）。tfdt 补丁、commit 顺序、EXTINF 口径不变。旧的 `write_mp4v` / `transcode` 这一批先不删，单独提交删除。这是 15 路改造的第 ③ 阶段，见 [20261013_SCALE_15_STREAMS_PROPOSAL.md](20261013_SCALE_15_STREAMS_PROPOSAL.md)。

## 变更背景

- **现状**：录像段队列只有一个消费线程，而且必须单线程（tfdt 按执行顺序累计）。每段先编一次 mp4v，再转码一次 libx264，实测串行吞吐 351 帧/s。15 路的需求是 raw 450 帧/s + processed 225 帧/s，合计 675 帧/s，跟不上就会持续积压，最终丢段，证据永久缺失。
- **为什么不加 worker**：多 worker 会破坏「同一 run 的段按提交顺序执行」这个前提，所以先把单段编码做快。

## 方案详情

### 全景

```text
旧：frames ─► cv2.VideoWriter(mp4v, fps) ─► stage/source.mp4 ─► ffmpeg -i source.mp4 → libx264 fMP4
新：frames ─► ffmpeg -f rawvideo -pix_fmt bgr24 -s WxH -framerate <effective_fps> -i pipe:0 → libx264 fMP4
              （stdin 逐帧写；stderr 写进 stage/ffmpeg.log；输出参数与旧 transcode 共用 `_output_args()`）
```

| 部件 | 落在哪 |
|------|--------|
| `encode(stage, frames, fps)`、`_output_args()` | [_fmp4.py](../../app/storage/hls/_fmp4.py) |
| ① stage 改调 `_fmp4.encode` | [_write.py](../../app/storage/hls/_write.py) |
| 假编码替身改为 patch `_fmp4.encode`；新增输入校验与 stdin 端到端用例 | [test_storage_hls.py](../../tests/test_storage_hls.py) |

### 方案选型

| 方案 | 结论 |
|------|------|
| 帧经 stdin 单次编码（采用） | 少一次编码和一次文件读写；ffmpeg 子进程编码时不占主进程 GIL |
| 保留两次编码，按 task 分片并行 | 不采用：CPU 编码量翻倍，还要另外证明分片不破坏 run 内顺序。留作 15 路验收不达标时的储备 |
| NVENC | 不采用：和推理争 GPU；GeForce 卡有并发编码会话上限 |

### 实现约束（不遵守会静默出错或卡死）

- **`-framerate` 用 `effective_fps(frames)`**：和 EXTINF 同源，tfdt 由 EXTINF 累加而来，三者一致。
- **stderr 不走管道，写进 stage 里的日志文件**：一边写 stdin、一边不读 stderr 管道，会在 ffmpeg 报错多时双方互相堵死。
- **写前检查帧尺寸**：rawvideo 按首帧尺寸切帧，混进不同尺寸的帧会让整段错位而不报错，所以起 ffmpeg 之前先抛 `ValueError`。
- **非连续帧先转连续**：切片或渲染产出的视图可能不是 C 连续，直接写会导致字节错乱。
- **超时**：`_TIMEOUT_S` 到点由定时器 kill ffmpeg，抛 `TimeoutExpired`。ffmpeg 提前退出后写端会报 BrokenPipe / EINVAL，这里吞掉，统一按退出码报错。
- **失败语义不变**：异常原样上抛，`insert_segment` 删掉 stage、不登记，录像服务丢这一段、不重试。

## 变更效果

调用真 `hls.insert_segment`，640×480、每段 300 帧，6 次取中位数（本地，进程锁在 P 核）：

| 指标 | 变更前 | 变更后 |
|------|--------|--------|
| raw 段（30fps）单段耗时 | 0.63s | 0.22s |
| processed 段（15fps，同为 300 帧）单段耗时 | 1.08s | 0.23s |
| 单条队列串行吞吐 | ≈ 351 帧/s | ≈ 1333 帧/s |
| 15 路需求 675 帧/s | 不够 | 余量约 2 倍 |

> 提案里写的「现行 ≈ 486 帧/s」是按 processed 每段 150 帧估算的。实际 processed 每段也是 300 帧，所以真实的旧吞吐更低。

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_storage_hls.py`（含真 ffmpeg 端到端：两段 EXTINF / tfdt / sidecar、像素帧号往返解码闭合；新增：帧尺寸校验、非连续帧、ffmpeg 失败带 stderr 且不卡死、超时 kill） | 176 passed |
| 全量 `pytest tests/` | 1006 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 只在 Windows + 项目自带 ffmpeg 上实测 | Linux 管道和 ffmpeg 行为预期一致 | 测试环境回放抽查 raw / processed 两轨，确认无跳段、时长对齐 |
| 旧路径 `write_mp4v` / `transcode` 仍在 | 死代码 | 下一批单独删除 |
| 解码、编码、渲染同时满载时的整机 CPU 没测过 | 16 核机器上可能互相争抢 | 15 路验收时观察整机 CPU |
