# 15 路并发分阶段改造提案：主进程减负、内存按字节兜底、录像单次编码、渲染进程化

> **变更状态**：进行中（2026-10-13）　① – ⑤ 已在 `feature/scale-15-streams` 落地，各阶段记录见下表「详见」列对应的 update；⑥ 15 路验收压测待在测试环境进行。各节数字已按落地实测更新，选型表保留选型期实验值
> **知识库**：无需沉淀（提案；落地记录另行沉淀）

## 概述

目标是医院内网单机 15 路并发（每路解码 30fps、推理与渲染 15fps），不动前后端契约。分 6 个阶段、8 批提交：解码直写帧数组 → 内存按字节兜底 → 录像单次编码 → 推理送帧改共享内存环 → 渲染挪进子进程 → 15 路验收压测。本文记录每项决策、被否掉的备选方案，以及支撑决策的实验数据。

## 变更背景

- **现状**：生产上验证过的上限是 4 路。主进程里同时跑着解码读线程、推理送帧、渲染、录像的 mp4v 中间编码，这些都争同一把 GIL。
- **触发来源**：需求是医院内网部署、最大 15 路并发、不改前后端契约。
- **部署假设与已定参数**（2026-10-13 与负责人确认）：
  - **服务器**：按中等性能服务器规划，即同构 x86 CPU（不分大小核）、16 物理核、32GB 内存、单块 4090 级 GPU。本文实验在 8 个 P 核（16 线程）上完成，CPU 侧不高于这一假设。
  - **摄像头帧率**：实际 30fps，与 `raw_fps = 30` 一致，不改配置（「配置写 30、实际 25」只是测试视频的情况）。
  - **`ca_maxlen_seconds` 从 30 降到 20**：随 ② 落地，见 §2。
  - **渲染子进程数**：`viz_processes = 2`，见 §5。
- **要排除的旧线索**：issue #82（双路时管理页帧率 15↔3 跳变）的根因是前端定时器泄漏。当时双路的服务端实测满额（渲染线程 tick 289–297/300，单帧渲染 12–15ms），所以不把它当作瓶颈依据。

本地实验确认的问题如下。数据见方案详情各节；「现行」指当前代码，「15 路」指合成负载。

| 编号 | 问题 | 15 路下实测 | 风险 |
|------|------|------------|------|
| #1 | 解码读线程每帧在持有 GIL 时拼帧（三次整帧拷贝 + 两次大块新分配），单线程测 0.80–0.94ms/帧 | 解码 13.9–21.7fps（应为 30），送推理 103–162 帧/s（应为 225） | 推理断档；解码积压在 RTSP 侧 |
| #2 | 渲染在单个线程里，瓶颈是 GIL 而非算力：开 1/2/4/8 个线程，总吞吐 178/170/165/159 帧/s | 现行架构 4 路 13.4、6 路 9.2、8 路 2.3、15 路 0.4 fps/路 | processed 轨和实时画面断帧 |
| #3 | 录像只有一条串行队列，每段先编 mp4v 再转码 libx264，编码两次 | 吞吐约 351 帧/s，需求 675 帧/s | 持续积压，最终丢段（证据永久缺失） |
| #4 | 录像队列按段数限长：100 段 × 285MB | 转码停滞时最多占 28.5GB 才开始丢段 | 在丢段之前先 OOM |
| #5 | 推理送帧走 `mp.Queue` pickle | 主进程 1.1–1.9ms/帧 | 主进程 CPU 余量被吃掉 |

### 实验环境与可信度

- **硬件**：i7-14650HX（8P+8E，24 个逻辑核），32GB，Windows 11，Python 3.11，OpenCV 4.11，Pillow 11.3。glibc 实验在 WSL2 Ubuntu 上跑（glibc 2.39）。
- **锁 P 核**：所有进程都锁在 P 核上跑。不锁的话，同一段代码会因为被调度到 P 核还是 E 核，耗时相差约 4 倍。
- **合成负载的组成**：真实 ffmpeg 解码（`-re` 30fps，640×480 bgr24）；读线程照搬现行写法；推理送帧照搬 `RemoteInferProxy`（子进程只收帧，不推理）；渲染用现行的 `FixedVisualizer`。**没有覆盖** GPU 推理、时序 Actor、WS 推流、uvicorn，所以结论偏乐观。
- **波动**：同一配置跨批次运行时，绝对值能差好几倍（例如「readinto + 渲染线程 + Queue」一批测出渲染 2.2–2.5fps，另一批 10.3–10.7fps）。只采信方向一致、能多次复现的结论，最终以生产机上的 15 路压测为准。
- **脚本位置**：仓库外的 `src/_bench_viz/`，不入库。

| 脚本 | 测什么 |
|------|--------|
| `bench_render.py` | 单帧渲染耗时拆分、多线程扩展性、文字区域优化 / 字形缓存原型 |
| `bench_mainproc.py` | K 路解码 × 读法 × 渲染方式 × 送帧方式的主进程负载矩阵 |
| `bench_decode.py`、`micro_copy.py` | 解码加速的来源：读调用次数 vs 拼帧拷贝 |
| `bench_ipc.py` | `mp.Queue`、TCP 回环、共享内存三种跨进程送帧方式的主进程成本 |
| `bench_hls.py` | 录像段编码：现行两次编码 vs 帧经 stdin 单次编码，含串行与并行吞吐 |
| `malloc_churn.py` | glibc 下每帧新分配、跨线程释放时 RSS 是否膨胀 |

## 方案详情

### 全景：改造后的主进程数据流

```text
RTSP ─► ffmpeg 解码子进程 ─pipe─► [主进程] 读线程：readinto 直写每帧新数组 ①
                                        │
           ┌────────────────────────────┼──────────────────────────────┐
           ▼                            ▼                              ▼
   ca_ready（独立浅上限 ②）     ca_raw / ca_processed（攒满一段）     最新原始帧
           │                            │ sweeper 每 1s 拉整段          │
           ▼                            ▼                              ▼
   推理代理：帧拷进共享内存环，   录像队列：按字节限额 ②；           渲染代理：帧拷进共享内存槽，
   队列只传槽位号 ④              帧经 stdin 灌 ffmpeg 单次编码 ③       RenderSpec 走队列 ⑤
           │                                                           │
           ▼                                                           ▼
   推理子进程（GPU）                                      渲染子进程 ×P → 结果拷回主进程
                                                          → ca_processed / 实时画面快照（契约不变）
⑥ 15 路验收压测（生产机）
```

**阶段之间的硬约束**：

- **生产路数**：① ② ③ 都落地之前，生产不把并发提到 4 路以上。①③ 解决吞吐，② 解决内存安全，三者缺一都会在 15 路下出事。
- **④ 先于 ⑤**：共享内存环组件先在推理侧落地并测绿，渲染侧再复用。推理代理现成，影响面更小。
- **每阶段的门槛**：本阶段 `pytest tests/` 全绿，并用同一套基准测出前后对比，再进入下一阶段。较大的 ④ ⑤ 按 [DEVELOPMENT.md §6](../DEVELOPMENT.md) 的四步走：新实现独立落地 → 新实现测绿 → 迁移调用点 → 单独删除旧实现。

| 阶段 | 批次 | 落在哪 | 详见 |
|------|------|--------|------|
| ① 解码直写帧数组 | 1 批 | `app/services/stream/decoder.py` | §1 |
| ② 内存按字节兜底 | 1 批 | `app/settings.py`、`app/services/client/{config,queues}.py`、`app/services/recording/service.py`、`app/main.py`（启动自检） | §2 |
| ③ 录像单次编码 | 2 批（切换 + 删旧） | `app/storage/hls/{_encode,_fmp4,_write}.py` | §3 |
| ④ 推理送帧改共享内存环 | 2 批（组件 + 迁移/删旧） | 新增 `app/services/utils/frame_ring.py`；`app/services/inference/online/detection/{infer_proxy,stage_worker}.py` | §4 |
| ⑤ 渲染挪进子进程 | 2 批（代理 + 迁移/删旧） | `app/services/inference/online/visualization/` | §5 |
| ⑥ 验收压测 + 条件项 | — | 测试环境 / 生产机 | §6 |

各阶段落地记录：① [DECODER_READINTO](20261013_DECODER_READINTO.md)　② [MEMORY_BUDGET](20261013_MEMORY_BUDGET.md)　③ [HLS_STDIN_SINGLE_ENCODE](20261013_HLS_STDIN_SINGLE_ENCODE.md) + [HLS_DROP_MP4V_PATH](20261013_HLS_DROP_MP4V_PATH.md)　④ [SHARED_FRAME_RING](20261013_SHARED_FRAME_RING.md) + [INFER_SHM_RING](20261013_INFER_SHM_RING.md)　⑤ [VIZ_RENDER_PROXY](20261013_VIZ_RENDER_PROXY.md) + [VIZ_RENDER_PROCESSES](20261013_VIZ_RENDER_PROCESSES.md)

### 方案选型

| 决策点 | 方案 | 实测依据 | 结论 |
|--------|------|---------|------|
| D1 解码读法 | 每帧 `np.empty` + `readinto` 直写 | 拼帧的 Python 侧开销 3–4µs/帧（现行 796–943µs）；15 路解码 30.0fps | **采用** |
| | `BufferedReader.read(整帧)` | 效果接近，但多一层内部缓冲，渲染余量略低（9.9 vs 10.3–10.7fps/路） | 否 |
| | 复用同一块固定缓冲 | — | 否：ca_raw 最长保留 30 秒，ca_ready、最新帧快照、录像都持有帧引用，复用会静默覆盖还在排队的帧 |
| | Linux 加大管道 `F_SETPIPE_SZ` | 读调用次数不是成本来源：按 32KB 读和一次要整帧，都是 29 次读调用/帧，收益相同 | 否 |
| D2 渲染 | 渲染放进子进程 | 15 路渲染 13.2–14.7fps/路（2 个子进程），测量方法本身上限 14.4–14.7 | **采用** |
| | 多开渲染线程 | 1→8 线程，总吞吐 178→159 帧/s，不随线程数增长 | 否 |
| | 只对文字区域做色彩转换 | 2.49→2.43ms，无收益（整帧转换只占 0.7ms） | 否 |
| | 字形缓存 | 单线程快 40%，但有 GIL 竞争时同样崩；锚点和像素不完全一致 | 否 |
| | 降低 processed 帧率 | — | 否：改变产品行为（processed 轨、实时画面帧率） |
| D3 跨进程送帧 | 共享内存环，队列只传槽位号 | 满速约 9,700 帧/s，主进程 0.10–0.13ms/帧（≈一次整帧 memcpy） | **采用** |
| | `mp.Queue`（现行） | 满速 434–805 帧/s，主进程 1.1–1.9ms/帧 | 否 |
| | Redis / TCP | TCP 回环（Redis 的下界，少了服务端那一跳）满速只有 168–186 帧/s，达不到 225 的需求；还要多部署一个组件 | 否：跨机器、多消费者、持久化这些需求都不存在 |
| D4 录像编码 | 帧以 rawvideo 经 stdin 灌 ffmpeg，单次编码 | 串行吞吐 4.58 段/s ≈ 1030 帧/s（按 processed 150 帧/段估；落地实测见 §3），需求 675 帧/s | **采用** |
| | 保留两次编码，按 task 分片并行 | 2 个 worker 3.78 段/s ≈ 850 帧/s，但 CPU 编码量翻倍 | 留作储备（§6） |
| | NVENC | — | 否：和推理争 GPU，GeForce 卡有并发编码会话数上限，CPU 方案已够用 |
| D5 内存兜底 | 录像队列按字节限额 + ca_ready 独立上限 + 启动自检 | 见 §2 | **采用** |
| | 调 malloc 参数 | `MALLOC_ARENA_MAX=2` 全部释放后仍有 2.74GB 不归还系统；默认参数 RSS/存活 = 1.06，可完全归还 | 否：默认参数不动 |

### 1. `stream/decoder.py` — 解码直写帧数组（全景 ①）

#### 旧

```python
chunk = stdout.read(self.chunk_read_size)          # 32KB
self.buffer.extend(chunk)
while len(self.buffer) >= self.frame_size:
    frame_bytes = bytes(self.buffer[: self.frame_size])   # 切片拷一次 + bytes 再拷一次
    del self.buffer[: self.frame_size]
    arr = np.frombuffer(frame_bytes, dtype=np.uint8)
```

#### 新

```python
arr = np.empty(self.frame_size, dtype=np.uint8)    # 每帧新数组，不复用
mv, got = memoryview(arr), 0
while got < self.frame_size:
    n = stdout.readinto(mv[got:])                  # 内核直接写进帧数组，期间不持 GIL
    if not n:
        return                                     # EOF：残帧丢弃，与现行语义一致
    got += n
std = arr.reshape((self.height, self.width, DEFAULT_CHANNELS))
```

> **加速来源**：两种写法都是每帧 29 次读调用，因为 ffmpeg 每次只往管道写 32KB。加速来自拼帧不再持有 GIL：现行写法每帧在 GIL 内做三次整帧拷贝和两次约 0.92MB 的新分配，首次写入新内存的缺页也发生在 GIL 内；readinto 的首次写入发生在内核里。
> **不动的部分**：背压判定、降采样、`Frame.timestamp` 取值时机、指标埋点。`chunk_read_size` 配置项随之失效，同批删除。

| 指标（15 路，渲染线程 + Queue 送帧） | 现行 | 改后 |
|------|------|------|
| 解码 fps（两批） | 13.9 / 21.7 | 30.0 / 30.0 |
| 送推理 帧/s（需求 225） | 103 / 162 | 225 / 224 |

**落地实测**（真实 `FFmpegDecoder` + `ClientQueues`，15 路，不加送推理负载）：解码改前改后都是 30fps，收益体现在 GIL 余量上——同进程渲染线程吞吐 141 → 288 帧/s。

### 2. 内存按字节兜底（全景 ②）

**15 路内存预算**（单帧 0.92MB；raw 段 300 帧 = 10s，processed 段也是 300 帧，按 15fps 即 20s）：

```text
情形                          现行                                   改后
正常运行（各路段边界对齐）     15 ×（raw 285MB + processed 285MB）= 8.5GB   不变（7.7GiB）
录像停滞（sweeper 正常）       100 段 × 285MB = 28.5GB + 8.5GB ≈ 37GB    预算 8GiB + 7.7GiB ≈ 15.7GiB
推理停滞                      ca_ready 900 帧（与 ca_raw 大部分不共享）≈ +8–11GB    ca_ready 30 帧 ≈ +0.4GB
sweeper 停滞（三队列都满）     15 × 3 × 900 帧 ≈ 37GB                   15 × 2 × 600 帧 + 0.4GB ≈ 15.9GiB（ca_maxlen 20s）
最坏值                        ≈ 37GB                                  ≈ 15.9GiB + 进程基线
```

几种停滞不会同时发生：sweeper 活着时 ca 队列会持续被拉空；sweeper 停了，录像队列也就不再进段。

- **2a. ca_ready 独立浅上限，`ca_maxlen_seconds` 从 30 降到 20**：新增 `settings.ca_ready_seconds`（默认 2，帧数 = 秒数 × inference_fps = 30）。解码背压改为看 `ca_ready.maxlen`。ca_raw / ca_processed 仍沿用 `ca_maxlen_seconds`，改为 20 后是 600 帧，仍满足现有的「≥1.2 倍段长（360 帧）」校验，raw 轨给录像留 2 段余量。依据：调度器取帧速度远高于输入（约 1000fps 对 225fps），ca_ready 正常时总是空的；积压 60 秒的旧帧对实时督促也没有用。
- **2b. 录像队列按字节限额**：`RecordingService` 记录每条队列在途的字节数，`submit_segment` 超过 `recording_queue_max_mb` 即拒收。拒收沿用现有的「丢段 + warning」语义，只是触发条件从段数变成字节；在途字节在 `_write` 结束时扣减（不论成败）。默认 `queue_max_mb = 8192`，约一轮对齐突发 15 × (285 + 285)MB。保留段数上限作双保险。
- **2c. 启动自检**：新增 `settings.capacity_planned_streams`（默认 15），**只用于预算，不拦截 `/api/start`**，所以不改契约。启动时按上表的最坏值公式加上进程基线，与 `/proc/meminfo` 的 `MemAvailable` 比较，超了拒绝启动，并在报错里给出各项构成。这条遵循知识库「靠量级差才不触发的冲突，写成启动校验」。Windows 开发机跳过并打 warning。
- **malloc 参数不动**：WSL 模拟 15 路持续分配和跨线程释放，60 秒内 RSS/存活稳定在 1.06，释放后全部归还（0.01GB）。

| malloc 配置 | 稳态存活 | 稳态 RSS | RSS/存活 | 全部释放后 RSS |
|------|------|------|------|------|
| glibc 默认 | 5.83GB | 6.18GB | 1.06 | 0.01GB |
| `MALLOC_ARENA_MAX=2` | 5.87GB | 6.36GB | 1.08 | 2.74GB |
| `MALLOC_MMAP_THRESHOLD_=131072` | 6.06GB | 6.10GB | 1.01 | 0.01GB |

**32GB 下的余量**：最坏约 15.9GiB，加上进程基线 `memory_baseline_mb = 6144`（估计值：主进程里时序 GRU 用的 torch、推理子进程的 CUDA 上下文、2 个渲染子进程、15 个 ffmpeg 解码进程），合计约 21.8GiB。32GB 的机器开机后 `MemAvailable` 约 29GiB，启动自检能通过，还剩约 7GiB 余量。如果保持 30s，最坏加基线约 29.5GiB，会被启动自检拒绝，这就是降到 20s 的依据。

### 3. `storage/hls` — 录像单次编码（全景 ③）

#### 旧

```text
frames ─► cv2.VideoWriter(mp4v, fps=effective_fps) ─► stage/source.mp4 ─► ffmpeg -i source.mp4 -c:v libx264 … fMP4
```

#### 新

```text
frames ─► ffmpeg -f rawvideo -pix_fmt bgr24 -s WxH -framerate effective_fps -i - -c:v libx264 … fMP4
          （stdin 逐帧写入；其余参数与现行 transcode 一致）
```

> **必须保留的约束**：`effective_fps` 只算一次，同一个值既给 `-framerate` 又给 EXTINF；子进程 `cwd=stage`，输出只传 basename；`video_track_timescale` 继续锁死；转码完成后照旧 hex 补丁 tfdt；单条录像队列的执行顺序（tfdt 依赖它）不变。
> **失败语义**：ffmpeg 中途退出 → 写 stdin 报 BrokenPipe → 和现行的转码失败一样记 error、丢这一段，不重试。
> **迁移**：新增按帧转码的函数并测绿（检查 EXTINF、tfdt、帧数和现行产物一致，复用 `test_storage_hls.py` 的端到端用例）→ `insert_segment` 切换过去 → 删除 `write_mp4v` 和 mp4v 中间文件。

落地实测（真 `hls.insert_segment`，640×480，6 次取中位数）：

| 指标 | 现行 | 单次编码 |
|------|------|---------|
| 单段耗时 raw 300 帧（30fps） | 0.63s | 0.22s |
| 单段耗时 processed 300 帧（15fps） | 1.08s | 0.23s |
| 单条队列串行吞吐 | ≈ 351 帧/s | ≈ 1333 帧/s |
| 15 路需求 | 450（raw）+ 225（processed）= 675 帧/s | 同左，余量约 2 倍 |

> 选型期按 processed 每段 150 帧估算出「现行 486 → 单次编码 1030 帧/s」；processed 实际每段也是 300 帧，真实的旧吞吐更低。

### 4. 推理送帧改共享内存环（全景 ④）

- **组件**：新增 `app/services/utils/frame_ring.py`，提供 `SharedFrameRing`（固定槽位大小 = 单帧字节数；空闲槽位表；名字带 pid）。它属于通用并发件，不 import 任何具体服务。
- **槽位数**：在途批数上限 × 最大批大小 = 8 × 4 = 32 个槽、约 29MB，替代现行 `mp.Queue` 缓冲（上限约 177MB）。帧尺寸超出槽位时整批退回 pickle 直送，不卡死在拒收上。
- **数据流**：`submit` 把帧拷进空闲槽，请求只带 `(req_id, 槽位号列表, 形状)`；子进程按名字映射这块共享内存，原地读取。
- **槽位回收（防泄漏）**：收到对应响应、`pending.pop` 时归还；`submit` 送不进去回滚时归还；子进程重启清空 pending 时，全部在途槽位归还。没有空闲槽时按现有的「在途满即拒收、计丢帧」处理。
- **残留清理**：停机时 `close` + `unlink`；主进程被 SIGKILL 时由 multiprocessing 的 resource_tracker unlink。启动时扫 `/dev/shm` 清残段没有做——只有主进程连同 resource_tracker 一起被硬杀才会残留，残段名带 pid 前缀可手工清。

| 送帧方式（满速、无其他负载） | 吞吐 | 主进程 CPU/帧 |
|------|------|------|
| `mp.Queue`（现行） | 434 / 805 帧/s | 1.1–1.9ms |
| TCP 回环（Redis 的下界） | 168 / 186 帧/s | 1.5–1.7ms |
| 共享内存环 | ~9,700 帧/s | 0.10–0.13ms |

> **定位：给主进程留余量，不是硬前提**。在「readinto + 2 个渲染子进程」的完整场景里，两批测量中 Queue 和共享内存的渲染帧率一批有差别（11.3 对 13.2），一批没有（14.6 对 14.7）；但主进程 CPU 始终是共享内存更低（0.47–0.81 核对 0.71–1.36 核）。真实主进程还有基准没覆盖的负载，所以保留这一阶段。

### 5. 渲染挪进子进程（全景 ⑤）

- **新增 `RemoteRenderProxy`**：照搬 `RemoteInferProxy` 的模式，包括 spawn 子进程、req_id 加 pending、监督线程退避重启、按 terminate → join → kill 三步收子进程。
- **主进程这边不变**：`VisualizationWorker` 的轮询、去重、吞吐统计不变。`prepare_visualization_data` 仍在主进程生成 `RenderSpec`（小数据类，走队列）；原始帧拷进共享内存槽，子进程原地渲染写回同一槽位。
- **每 run 单飞**：同一 run 同时最多一个在途渲染，保证 processed 帧按 ts 顺序进 `ca_processed`。
- **写回**：主进程在下一个 tick 把槽位结果拷成新数组，再走现行的 `cq.append_ca_processed` 和 `set_latest_rendered`。processed 轨、实时画面、HLS 都不变。
- **防迟到写**：请求上下文带 cq 句柄，写回经 cq 写门，非 ACTIVE 拒收，和推理写回同一套门禁。
- **就地渲染兜底**：池未就绪、槽满或帧放不进槽位时，在 viz 线程就地渲染（旧路径）；`viz_processes = 0` 时全部就地渲染。
- **子进程挂掉**：在途渲染丢弃，计入 `frame_drop_total{reason="viz_child_down"}`。
- **子进程只 import 渲染器和 render 类型**：不能拉起 torch / ultralytics，用导入门禁测试锁住。单个子进程常驻约 100–150MB。
- **子进程数**：`settings.viz_processes = 2`（已定）。落地实测 2 个子进程每路 12.6–13.2fps，3–4 个也只到 13.8fps，均低于 ⑥ 的 14fps 达标线；选型期测得的测量方法上限是 14.4–14.7。达标线保持 14fps 不变（2026-10-13 与负责人确认），⑥ 不达标时按 §6 的归因处理，不直接加进程。

落地实测（真实组件 15 路：`FFmpegDecoder` + `ClientQueues` + 模拟推理每帧 3+3 框 + 真实 `VisualizationWorkerPool`，每组 12s，已含 ① ④）：

| 渲染子进程数 | processed 出帧 fps/路 平均（最低） | 主进程 CPU |
|------|------|------|
| 0（线程内，旧路径） | 6.0 (6.0) / 6.7 (6.7) | 1.57 / 1.58 核 |
| **2（默认）** | **12.6 (10.7) / 13.2 (11.3)** | **0.73 / 0.86 核** |
| 3 | 13.8 (13.3) | 0.74 核 |
| 4 | 13.8 (13.4) | 0.81 核 |

### 6. 15 路验收压测与条件项（全景 ⑥）

验收在生产机上用 `integration_tests/test_multi_client.py --max-tasks 15` 跑。这个测试要读 DB，跑之前先与负责人确认。

| 指标 | 达标线 | 数据来源 |
|------|--------|---------|
| 解码 | 每路 ≥29.5fps，`ingress_backpressure` 丢帧为 0 | decoder 统计日志、`frame_drop_total` |
| 推理 | 每路 ≥14fps，`infer_backlog` 不持续增长 | `[PRESSURE]`、`infer_latency_ms` |
| 渲染 | 每路 ≥14fps，不出现 viz-starved | `[VIZ_THROUGHPUT]` |
| 录像 | 零丢段 | recording 的丢段 warning（带在途 MB / 预算）、`frame_drop_total{reason="recording_budget"}` |
| 内存 | 30 分钟内 RSS 稳定在 §2 的预算内；按实测回填 `memory_baseline_mb` | 进程 RSS |
| 接口 | `/task/live` P95 < 200ms | 压测侧统计 |

**渲染不达标时先归因再动参数**（本地 2–4 个子进程都没到 14fps，见 §5）：看 `[VIZ_THROUGHPUT]` 的标注——

| 归因 | 含义 | 处理 |
|------|------|------|
| render-bound | 子进程渲染耗时逼近出帧间隔 | 调大 `CLEANSIGHT_VIZ_PROCESSES` |
| supply-bound | 渲染够快，新推理结果供不上 | 查推理吞吐（转推理条件项），加渲染进程无效 |
| viz-starved | 主进程 viz 线程 tick 不足 | 查主进程 GIL 占用 |

**条件项**：只有验收不达标，且先做实验确认是对应环节的问题时才做。

| 条件项 | 触发条件 | 现有依据 |
|------|------|------|
| 录像按 `task_id` 哈希分片并行 | 录像队列在途字节持续增长 | 单次编码 + 2 个 worker 实测 5.01 段/s；同一 run 落在同一分片，tfdt 依赖的顺序不变 |
| WS 实时画面每帧只编一次 JPEG、编码挪出事件循环 | 接口 P95 不达标，且同时观看的连接多 | 单帧 JPEG + base64 2.2ms，现在每个观看连接各自在事件循环上编码 |
| GPU：FP16 / 加大组批 / TensorRT（engine 由训练侧产出） | 推理不达标 | **没做过实验**，GPU 余量未知 |
| 后端锁 P 核（`taskset`） | 实际部署机是大小核 CPU（与同构服务器的假设不符时） | 本机不锁核时，同一段代码耗时差约 4 倍 |

### 7. 保留项（不改动）

- **前后端契约**：全部 HTTP / WS 路径和返回格式（WS 仍是 `data:image/jpeg;base64,…`），HLS 布局与时间轴，盘上 run 目录。
- **`/api/start` 不新增拒绝条件**：`capacity_planned_streams` 只用于启动自检的预算。
- **语义不变**：三条 ca 队列的语义；背压丢帧和录像丢段的语义（只是触发条件从段数改为字节）。
- **录像单队列执行顺序**：分片只作为条件项，而且按 run 归属分片。
- **processed 段长**：300 帧（15fps 下 20s），本提案不动。

## 变更效果（本地落地实测，以 ⑥ 为准）

| 维度（15 路） | 改前 | ① – ⑤ 落地后 | 来源 |
|------|------|------|------|
| 解码 fps | 13.9–21.7 | 30.0 | §1 合成负载 |
| 送推理 帧/s（需求 225） | 103–162 | 225 | §1 合成负载 |
| 主进程送推理 CPU/帧 | 1.1–1.9ms | 0.10–0.13ms | §4 |
| 渲染 fps/路（达标线 14） | 6.0–6.7（①④ 已落地、渲染仍在线程内） | 12.6–13.2（最低 10.7–11.3） | §5 |
| 主进程 CPU | 1.57 核 | 0.73–0.86 核 | §5 |
| 录像串行吞吐（需求 675 帧/s） | ≈351 帧/s | ≈1333 帧/s | §3 |
| 最坏内存 | ≈37GB | ≈15.9GiB + 基线 6GiB ≈ 21.8GiB（32GB 机器） | §2 |

> 选型期合成负载下（旧解码 + Queue 送帧 + 线程渲染）渲染只有 0.4–0.7fps/路；上表「改前」列是各阶段落地时的直接对照。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 实验在 Windows 笔记本上做，没覆盖 GPU 推理、时序 Actor、WS、uvicorn | 绝对值偏乐观 | 每个阶段落地后在生产机复测；以 ⑥ 为准 |
| 实际部署机与「中等性能服务器」假设不符（内存 <32GB、大小核 CPU、核数更少） | 启动自检可能拒绝启动；渲染、转码余量不足 | 部署时取 `lscpu`、`free -g` 核对；内存不足按 §2 的公式继续下调 `ca_maxlen_seconds`（下限 12s = 1.2 倍段长） |
| 编码、解码、渲染子进程同时满载时的整机 CPU 没测过（录像编码是单独测的） | 16 核机器上可能互相争抢 | ⑥ 时观察整机 CPU，需要时把 ffmpeg 编码线程数调小 |
| GPU 吞吐没做实验 | 15 路推理可能不达标 | ⑥ 不达标时，先单独做推理基准，再决定 FP16 / 组批 / TensorRT |
| 共享内存在主进程连同 resource_tracker 被硬杀后残留 | `/dev/shm` 泄漏 | 概率低，残段名带 pid 前缀可手工清；有实际需要再补启动清理 |
| 渲染子进程增加 CPU 占用（每个最多约 1 核） | 和 ffmpeg 转码争 CPU | ⑥ 时观察整机 CPU；默认 2 个子进程 |
| 本地渲染 2–4 个子进程都低于 14fps 达标线 | processed 轨与实时画面偶有跳帧 | ⑥ 按 §6 的归因表处理，达标线不降 |
| `memory_baseline_mb = 6144` 是估计值 | 估低了，自检会放过真实超限 | ⑥ 记录进程总 RSS 后回填 |
