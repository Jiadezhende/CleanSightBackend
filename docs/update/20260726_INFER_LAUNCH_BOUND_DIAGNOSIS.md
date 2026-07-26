# clean stage 单路 ~10fps 根因观测与改造提案（launch-bound，非 compute-bound）

> **变更状态**：观测已定位，改造未实现（2026-07-26）
> **知识库**：待沉淀 → `kb/SERVICE_INFERENCE.md`（改造落地后融合）
>
> 相关：[20260630_FRAME_DECIMATION_ACCUMULATOR_TASK.md](20260630_FRAME_DECIMATION_ACCUMULATOR_TASK.md)（降采样契约）、[20260628_INFER_PRESSURE_OBSERVABILITY.md](20260628_INFER_PRESSURE_OBSERVABILITY.md)（`[INFER_PRESSURE]` 积压观测）。

## 概述

- **现象**：单路流（`test/clean-test.mp4`，**960×544 / 25fps**）下，clean stage（stage=2）实测推理帧率 **~10fps**，低于降采样目标（`inference_decimation=2` → 名义 12.5fps；注：源实际 25fps，配置 `raw_fps=30` 对不上）。
- **根因**：**launch / dispatch-bound，不是 compute-bound**。模型确在 GPU 上，但每次前向 GPU **时钟拉满却 SM 空转 ~88%**——瓶颈是 CPU 端 kernel 发射 + host-device 同步的停顿，yolo11n + 960×544 帧太小喂不饱 4090。10fps ≈ 每帧 ~40–80ms「空等延迟」的倒数，**与降采样、组批、timeout 全无关**。
- **改造方向**：
  1. **先验证**：拆掉 [pool.py](../../app/services/inference/detection/pool.py) 的 CUDA-stream + 全局 `synchronize()`（对同步 predict 零并行收益，疑似注入同步停顿与方差），复用现有 `[DIAG]` 埋点重测。
  2. **若拆后仍慢（治本）**：导出 **TensorRT engine**，把上百次 kernel 发射融合，为 launch-bound 小模型的正解。
  3. 顺带：`raw_fps` 配置修正（写 30 实际 25）；组批自适应 timeout 清理（见附）。
- **影响面**：主改 pool.py；[detector.py](../../app/services/inference/detection/detector.py) 有临时 `[DIAG-TEMP]` 埋点待清理；bench 可选。**本提案不含已实现项。**

---

## 观测报告（dev 实测，cleansight-4090）

### 逐层排查链（每步都排除了一个假设）

| 步骤 | 观测 | 排除的假设 |
|------|------|-----------|
| 1. `[Worker-2] Batch processed` | `size=1~2`、`queue_depth=1~2`、supply 稳定却排不空 | → 推理追不上、积压。**非 supply/dispatcher/timeout** |
| 2. 同上 | batch=1 每帧 ~80ms；batch=2 每帧 31ms | 固定 per-call 开销可被组批摊薄（但单路凑不到 batch） |
| 3. `infer_latency_ms{model}` | clean_large P50 **39.6** / P95 91.3；clean_small P50 **24.4** / P95 48.0；两模型 162/162 **每帧都跑、串行相加** | 单帧 ≈ 40+24 ≈ 64ms（P50）、~139ms（P95） |
| 4. `[DIAG]`（detector.py 临时埋点） | `device=cuda:0`；`speed` = preprocess ~2ms / **inference 15–50ms** / postprocess ~2ms | → **非 CPU、非 preprocess、非 `predict()` 框架开销**；慢在 GPU forward 本身 |
| 5. `nvidia-smi dmon` | `pclk=2520MHz`（满 boost）、`mclk=10251`（满速）、`fb=2.2GB` 无他进程，**但 `sm=12~13%`、`pwr=~60W`**（4090 可达 450W） | → **时钟拉满却空转 88%**。非降频、非抢卡、**非 compute-bound** |

### 关键原始数据

`[DIAG]`（同一 worker 线程内两模型串行，外裹 pool 的 CUDA-stream + synchronize）：

```
[DIAG clean_small] device=cuda:0 n=1 speed={preprocess:2.0, inference:18.9, postprocess:1.7} wall=23.7ms
[DIAG clean_large] device=cuda:0 n=2 speed={preprocess:1.1, inference:21.2, postprocess:2.6} wall=50.6ms
[DIAG clean_large] device=cuda:0 n=1 speed={preprocess:2.4, inference:49.1, postprocess:3.6} wall=56.5ms
```

`nvidia-smi dmon -s pucm`（跑测试时）：

```
# gpu  pwr  sm   mem  mclk   pclk    fb
    0   18    0    0    405    210   2114   <- 空闲
    0   47   12    0  10251   2520   2268   <- 有活：时钟满、SM 仅 12%
    0   67   13    0  10251   2520   2268
    0   65    0    0  10251   2520   2268
```

### 结论：launch-bound

GPU 时钟满、显存充足、无竞争，但 SM 利用率仅 ~12%、功耗仅 ~60W —— **每次前向 GPU 大部分时间在等 CPU 逐个发 kernel + 反复同步**。yolo11n（~2ms 理论前向）在 4090 上被测出 15–50ms 且剧烈抖动，正是 launch-bound 的典型：小模型 + 小帧喂不饱卡，固定发射/同步开销主导。

**由此被否定、后人勿再走的方向**：`half=True`、换更小模型、降 `imgsz`、提高 decimation、调组批 timeout —— 全部针对 compute 或供给，而瓶颈是发射停顿，**均无效**。

---

## 待验证的分叉（决定改法）

那 ~18ms 的空等是 **app 内注入** 还是 **ultralytics 对小模型的固有发射开销**，二者改法不同：

- **拆掉 pool.py 的 CUDA-stream + 全局 `synchronize()` 后重测同一 `[DIAG]`**：
  - `inference` 掉到 ~2–3ms → 元凶是那套 stream/sync（自定义流对同步 predict 无并行收益，却让每次前向掺入跨流全设备同步）。改完即解。
  - 仍 ~20ms → 是 ultralytics/torch 跑此模型的发射开销本身 → 上 **TensorRT engine**。

（复用现有埋点即可，无需另跑独立 bench。）

---

## 改造方案（未实现）

1. **[pool.py](../../app/services/inference/detection/pool.py) 拆 CUDA-stream**：`_infer_batch_parallel_cuda` 的 `with torch.cuda.stream(...)` + 末尾 `torch.cuda.synchronize()` 改为两模型裸串行（或改真线程并行——predict GPU 段释放 GIL，可让两模型重叠为 max 而非 sum）。可回退，先验证。
2. **TensorRT engine**（若步骤 1 不足）：导出 `.engine` 加载，融合 kernel 发射，是 launch-bound 小模型稳定 sub-2ms 的正道。
3. **`raw_fps` 修正**：源实际 25fps，`app/settings.py` 写死 30 → 目标帧率对不上，另立小任务核。
4. **清理 [detector.py](../../app/services/inference/detection/detector.py) 的 `[DIAG-TEMP]` 埋点**（定位完删）。

## 附：组批链路旁支结论（次要，可随手清理）

排查中顺带核实了组批设计，两点可优化但非本次瓶颈：

- **[service.py](../../app/services/inference/detection/service.py) 的自适应 timeout（1/2/3ms 按 queue_depth 分档）实质为惰性代码**：供给由 dispatcher 10ms 轮询量化、每轮每客户端仅 1 帧，1~3ms 窗口内等不到下一轮；两档结果（即返/部分返）不依赖 timeout 值。可拍平为固定值。
- **组批收益来自队列深度/积压，不来自等待**：`size=2` 是积压凑出、非 timeout 等出。单路流无法稳定凑批，故长期落在慢路径。
