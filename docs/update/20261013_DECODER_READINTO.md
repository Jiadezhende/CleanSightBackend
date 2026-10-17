# 解码读循环改 readinto 整帧直写：拼帧移出 GIL

> **变更状态**：生效中（2026-10-13）
> **知识库**：已沉淀 → [SERVICE_STREAM.md](../kb/SERVICE_STREAM.md)、[SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md)（2026-10-17）

## 概述

`FFmpegDecoder` 的读循环由「`read(32KB)` → `bytearray` 拼帧 → `bytes` 切片」改为「每帧 `np.empty` + `readinto` 读满整帧」。删除 `chunk_read_size` 配置项（`DecoderConfig` 与 `config/stream_config.yaml`）。背压、降采样、时间戳、指标不变。15 路改造第 ① 阶段，见 [20261013_SCALE_15_STREAMS_PROPOSAL.md](20261013_SCALE_15_STREAMS_PROPOSAL.md)。

## 变更背景

- **现状**：旧读法每帧在持 GIL 时做三次整帧拷贝（extend、切片、`bytes()`）和两次约 0.92MB 的新分配，单线程测得 0.80–0.94ms/帧。15 路 450 帧/s 时，这部分持续占用 GIL，解码掉到 13.9–21.7fps，同进程的渲染和送推理一起被拖垮。
- **加速来源不是读调用次数**：两种写法都是每帧 29 次读调用，因为 ffmpeg 每次只往管道写 32KB。收益来自拼帧移出了 GIL：readinto 由内核直接写进帧数组，首写缺页也发生在内核里，Python 侧每帧只剩 3–4µs。

## 方案详情

### 全景

```text
ffmpeg stdout ─► _read_frame：np.empty(frame_size) + readinto 循环读满 ─► _process_frame(std)：背压 → ca_raw / ca_ready（不变）
                 EOF 或残帧 → None → 读循环退出（与旧语义一致，由 health_monitor 决定是否重连）
```

| 部件 | 落在哪 |
|------|--------|
| `_read_frame`（新增）、`_reader_loop`、`_process_frames` → `_process_frame` | [decoder.py](../../app/services/stream/decoder.py) |
| 删除 `chunk_read_size` | [stream/config.py](../../app/services/stream/config.py)、`config/stream_config.yaml` |

### 方案选型

| 方案 | 结论 |
|------|------|
| 每帧新数组 + readinto（采用） | 内核写入期间不持 GIL；数组归下游独占 |
| 复用同一块缓冲 | 否：ca_raw 最长保留 20–30s，ca_ready、快照、录像都持有帧引用，复用会静默覆盖在排的帧 |
| `BufferedReader.read(整帧)` | 否：效果接近，但多一层内部缓冲和一次拷贝 |
| Linux `F_SETPIPE_SZ` 加大管道 | 否：读调用次数不是成本来源 |

## 变更效果

真实 `FFmpegDecoder` + 真实 `ClientQueues`，15 路文件输入（`-re` 30fps，640×480），同进程另起一条渲染线程测 GIL 余量（本地，进程锁在 P 核）：

| 指标 | 变更前 | 变更后 |
|------|--------|--------|
| 解码 fps（15 路平均） | 30.0 | 30.0 |
| 同进程渲染线程吞吐 | 141 帧/s | 288 帧/s |

在加上推理送帧的合成负载下（提案 #1），解码从 13.9–21.7fps 恢复到 30fps，送推理从 103–162 帧/s 恢复到 225 帧/s。

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_decoder_read_frame.py`（新增：短读跨边界拼帧、EOF 残帧丢弃、每帧独立数组、读循环喂满后在 EOF 退出） | 4 passed |
| 全量 `pytest tests/` | 987 passed |

## 追加（2026-10-13）：解码帧恢复只读

- **问题**：旧读法的帧来自 `np.frombuffer(bytes)`，天然只读；改成 `np.empty` + `readinto` 后帧变成可写数组。同一数组被 ca_raw（录像）、ca_ready（推理）、最新帧快照（渲染 / 实时画面）共用，日后任何一处误做原地绘制都会静默污染全部下游。
- **改法**：[decoder.py](../../app/services/stream/decoder.py) 的 `_read_frame` 读满一帧后释放 memoryview，再置 `arr.flags.writeable = False`。
- **验证**：现有下游都不原地写帧（渲染先 `frame.copy()`）。用真实视频冒烟：只读帧经共享内存环写入、ffmpeg stdin 编码、`FixedVisualizer.render` 均正常，原地写入会抛 `ValueError`。全量 `pytest tests/` 1034 passed。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 只在 Windows 管道上实测过 | Linux 管道语义相同，预期一致 | 测试环境部署后看 decoder 日志 `received N frames` 的速率 |
| `ClientQueues.get_latest_frame()` 每次整帧 `copy()` | 渲染侧每帧多一次拷贝 | 第 ⑤ 阶段（渲染进程化）时一并处理 |
