# CleanSightBackend 架构文档

## 一、系统简介

CleanSightBackend 是一套面向工业现场的 AI 视觉巡检后端系统。系统接入现场摄像头的 RTSP 流，实时运行多阶段 YOLO11 目标检测模型，对检测结果进行时序分析，自动识别违规操作并上报告警，同时录制带标注和不带标注的双路 HLS 视频，供事后溯源。

**技术栈**：FastAPI · YOLO11 · PostgreSQL · FFmpeg · MediaMTX · asyncio + threading

---

## 二、整体模块地图与数据转发路径

```
外部 RTSP 推流方（摄像头）
      │ RTSP over TCP（公网端口 8004）
      ▼
┌──────────────────────────────────────────────────────┐
│  mediamtx_gateway（独立微服务）                       │
│  asyncio TCP 透明代理：IP 白名单 + 速率限制           │
│  可选：守护 MediaMTX 子进程（指数退避自动重启）        │
└──────────────────────┬───────────────────────────────┘
                       │ RTSP（内部端口 18004，MediaMTX）
                       ▼
┌──────────────────────────────────────────────────────┐
│  StreamService                                        │
│  per-client FFmpegDecoder                            │
│  ffmpeg -i rtsp://... -f rawvideo pipe:1             │
│  stdout → buffer → _process_frames()                 │
└──────────┬───────────────────────────┬───────────────┘
           │ FrameData（全帧率 30 fps） │ FrameData（降频 ~15 fps）
           │ ca_raw.append()           │ ca_ready.append_throttle()
           ▼                           ▼
┌──────────────────────────────────────────────────────┐
│  ClientManager + ClientQueues（per-client）           │
│                                                      │
│  ClientManager：task_id ↔ client_id 双向索引         │
│                                                      │
│  ClientQueues 共享内存中枢：                          │
│    ca_raw       原始帧环形缓冲（30 fps）              │
│    ca_ready     推理输入队列（~15 fps，SPSC）         │
│    ca_processed 渲染帧缓冲                           │
│    slide_window 每检测任务 10 s 时序历史              │
│    latest_*     原子快照（inference/temporal/render） │
│    alarm_log    100 条告警环形缓冲 + 序列号           │
└──┬──────────┬─────────────────────────┬──────────────┘
   │          │ 满段自动触发              │ 满段自动触发
   │ ca_ready │ ca_raw → raw HLS        │ ca_processed → proc HLS
   │（pop）   ▼                         ▼
   │   ┌─────────────────────────────────────────────┐
   │   │  PersistenceManager                         │
   │   │  HLSWorkerPool（2 线程）：ffmpeg → mp4 落盘 │
   │   │  AlarmWorkerPool（1 线程）：HTTP 告警上报    │
   │   │  CleanupWorker（可选）：清理 15 天前录像     │
   │   └─────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────┐
│  InferenceEngine                                     │
│                                                      │
│  Dispatcher（后台线程）                              │
│    Round-Robin 轮询所有客户端 ca_ready               │
│    按 Stage 分组 → 组成批次                          │
│            ↓ List[InferenceRequest]                  │
│  ModelWorkerPool（per-Stage 线程池）                 │
│    批量 GPU 推理（YOLO11）                           │
│    结果双写：                                        │
│      push_detection(slide_window)   时序历史         │
│      set_latest_inference(snapshot) 原子快照         │
│            ↓                                         │
│  TemporalActor（per-client，1 Hz）                   │
│    get_slide_window() → analyze_temporal()           │
│    set_latest_temporal(events)                       │
│    persist_alarm() ──────────────→ PersistenceManager│
│            ↓                                         │
│  VisualizationPool（~15 fps）                        │
│    get_latest_inference() + get_latest_temporal()    │
│    render() → append_ca_processed()                  │
│            └─ set_latest_rendered()                  │
└──────────────────────────────────────────────────────┘
                ↓ get_latest_rendered()
┌──────────────────────────────────────────────────────┐
│  Routers（FastAPI HTTP / WebSocket）                 │
│  WS  /ai/video          JPEG base64 实时推流（30 fps）│
│  POST /api/start        bind_task + start_stream     │
│  POST /api/terminate    cleanup_client（3 步清理）    │
│  WS  /task/msg          stage + detections + alarms  │
│  GET  /task/message     增量告警查询（since_seq）     │
│  GET  /health/status    系统指标 + Prometheus 导出    │
└──────────────────────────────────────────────────────┘
```

所有 HTTP / WebSocket 请求在进入路由前先经过 **GatewayMiddleware**（IP 白名单 · 速率限制 · 反扫描检测）。

---

## 三、服务启动顺序

`app/main.py` 使用三层嵌套 lifespan 管理启停顺序：

```
启动（由外到内）：
  1. GlobalHealthMonitor.start()        心跳监控，流断线重连
  2. InferenceManager.start()           推理三池 + PersistenceManager
  3. 应用就绪，接受请求

关闭（由内到外，lifespan 先进后出）：
  1. shutdown_event.set()               通知 WebSocket 断开
  2. InferenceManager.stop()            停推理三池，收集结算告警
  3. PersistenceManager.stop()          刷新剩余 HLS 段，排空告警队列
  4. GlobalHealthMonitor.stop()         停心跳监控
```

---

## 四、模块说明

### 4.1 mediamtx_gateway（独立微服务）

独立进程，通过 `python -m mediamtx_gateway.main` 启动，与主服务解耦。

- **RTSPProxy**：基于 `asyncio.StreamReader/Writer` 的 TCP 双向透明代理，将公网端口 8004 的连接转发到 MediaMTX 内部端口 18004。
- **安全层**：IP 白名单过滤 + 滑动窗口连接速率限制，超限直接 RST 断开。
- **进程守护**：可选启动并守护 MediaMTX 子进程，崩溃时指数退避重启（最多 5 次，间隔 2^n 秒上限 30 s）。

### 4.2 StreamService（流解码服务）

- 每个客户端对应一个 `FFmpegDecoder` 实例，以 FFmpeg 子进程读取 RTSP 流，通过 stdout 管道输出 rawvideo BGR24 像素数据。
- **跨平台读取**：POSIX 系统用 `selectors.DefaultSelector` 后台线程非阻塞读；Windows 用 daemon 线程阻塞读。
- **帧双写**：每帧数据分别写入 `ca_raw`（全帧率，用于 HLS 原始录制）和 `ca_ready`（限流至推理帧率，用于推理）。
- **背压控制**：当 `ca_ready` 队列深度超过容量 90% 时，仅丢弃推理帧，`ca_raw` 始终全量写入，保证录制完整。

### 4.3 ClientManager（全局客户端注册表）

- 全局单例，维护 `client_id → ClientQueues` 的映射，以及 `task_id ↔ client_id` 双向索引。
- 双向索引让路由层可以直接用 `task_id` 查到对应的 `ClientQueues`，无需遍历。
- 使用细粒度锁：已存在的客户端走快速路径（无全局锁），首次创建走 per-client 锁，避免并发重复创建。

### 4.4 ClientQueues（客户端状态中枢）

每个在线客户端对应一个 `ClientQueues` 实例，是各服务交换数据的共享内存中枢。

| 字段 | 类型 | 用途 | 写入方 | 读取方 |
|------|------|------|--------|--------|
| `ca_raw` | Deque[FrameData] | 原始帧（30 fps） | StreamService | 满段后触发 PersistenceManager |
| `ca_ready` | Deque[FrameData] | 推理输入（~15 fps） | StreamService | Dispatcher |
| `ca_processed` | Deque[FrameData] | 渲染帧 | VisualizationPool | 满段后触发 PersistenceManager |
| `_latest_rendered` | FrameData | 最新渲染帧快照 | VisualizationPool | WebSocket /ai/video |
| `_latest_inference` | InferenceResult | 最新推理结果快照 | ModelWorkerPool | VisualizationPool |
| `_slide_window` | Dict[task→Deque] | 10 s 时序历史 | ModelWorkerPool | TemporalActor |
| `_latest_temporal` | List[str] | 最新时序事件 | TemporalActor | VisualizationPool |
| `_alarm_log` | Deque[AlarmRecord] | 100 条告警环形缓冲 | TemporalActor | Routers 增量查询 |

`ca_raw` / `ca_processed` 帧数积累到 `ca_segment_len`（默认 300 帧 / 10 s）时，在锁外自动调用 `PersistenceManager.persist_hls_segment()`，不阻塞后续帧写入。

### 4.5 InferenceEngine（三池推理引擎）

由 `InferenceManager` 统一管理三个独立时钟的 Worker 池，各池之间通过 `ClientQueues` 的共享内存槽位通信，互不阻塞。

**Dispatcher（帧调度）**
- 后台线程以 Round-Robin 方式轮询所有客户端的 `ca_ready` 队列。
- 按当前客户端所处的检测 Stage（如 `LEAK`、`CLEAN`）将帧分组，凑满 `batch_size` 后交给对应的 `ModelWorkerPool`。

**ModelWorkerPool（模型推理）**
- 每个 Stage 对应一个线程池，持有一组 `Detector` 实例（无状态，多客户端共享）。
- 批量调用模型推理，结果双写回 `ClientQueues`：
  - `push_detection()` → `_slide_window`（供时序分析读取历史）
  - `set_latest_inference()` → `_latest_inference`（供可视化直接读取最新帧结果）

**TemporalActor（时序分析，1 Hz）**
- 每个客户端一个独立线程，1 Hz 定频 Tick。
- 持有该客户端专属的 `TemporalAnalyzer` 实例列表（有状态，维护各自的状态机）。
- 每次 Tick 读取 `_slide_window`，执行时序分析，产出实时告警并通过 `PersistenceManager` 上报。
- 任务终止时调用 `finalize()` 收集结算告警（如检测次数不足的违规判定）。

**VisualizationPool（可视化渲染，~15 fps）**
- 读取 `_latest_inference`（检测框）、`_latest_temporal`（时序事件）、最新原始帧。
- 叠加标注后写入 `ca_processed`（触发 HLS 录制）和 `_latest_rendered`（供 WebSocket 推流）。

**Workflow 扩展点**
- 新增检测类型只需在 `app/services/inference/workflows/` 下实现一对 `Detector`（无状态，负责单帧推理）+ `TemporalAnalyzer`（有状态，负责时序分析），并在 `inference_config.yaml` 中注册，无需修改其他代码。

### 4.6 PersistenceManager（持久化服务）

- **HLSWorkerPool**（2 个 worker）：接收 `HLSPersistenceTask`（帧列表），调用 FFmpeg 编码为 mp4 段文件落盘，分 `raw`（原始）和 `processed`（带标注）两类目录。
- **AlarmWorkerPool**（1 个 worker）：接收 `AlarmPersistenceTask`，HTTP POST 上报到外部告警系统，失败时重试。
- **StorageCleanupWorker**（可选）：每小时扫描存储目录，删除超过 15 天的录像文件。
- 两类 Worker 均使用有界队列（队列满时调用方阻塞），防止内存无限增长。

### 4.7 Routers（路由层）

| 端点 | 协议 | 职责 |
|------|------|------|
| `POST /api/start` | HTTP | 幂等启动：加 per-client asyncio.Lock，检测参数变化后全量重建 |
| `POST /api/terminate` | HTTP | 有序终止：stop_stream → remove_client（3 步） |
| `WS /ai/video` | WebSocket | 轮询 `_latest_rendered`，JPEG base64 推流，~30 fps |
| `WS /task/status/{client_id}` | WebSocket | 每秒推送任务状态 |
| `WS /task/msg/{client_id}` | WebSocket | 推送 stage + detections + recent_alarms |
| `GET /task/message/{task_id}` | HTTP | 增量告警（`since_seq` 参数），活跃任务走内存，历史任务查 DB |
| `GET /health/status` | HTTP | CPU / GPU / 队列指标 |
| `GET /metrics` | HTTP | Prometheus 格式指标 |

**任务启动幂等逻辑**：若 `task_id`、`current_step`、`rtsp_url` 三者均未变化，直接返回；任意字段变化则先执行完整清理再重建，避免资源泄漏。

**3 步有序清理**（`GlobalHealthMonitor.cleanup_client`）：
1. `StreamService.stop_stream(client_id)` — 停止 FFmpeg 子进程
2. `InferenceManager.remove_client(client_id)` — 停 TemporalActor，收集结算告警，刷新残余帧
3. `ClientManager.remove_client(client_id)` — 注销队列，释放内存

### 4.8 GatewayMiddleware（ASGI 安全中间件）

原生 ASGI 实现（非 `BaseHTTPMiddleware`），在 WebSocket 升级时不缓冲请求体。三层防护：

1. **IP 白名单**：白名单为空时放行所有来源；非空时仅允许列表内 IP，动态封禁优先于白名单。
2. **滑动窗口速率限制**：每 IP 每分钟最多 60 次请求；高频轮询路径（`/health`、`/task/message`）使用独立配额（600 次/分钟）；超限后计入违规计数，多次违规触发封禁。
3. **反扫描检测**：统计每 IP 在 5 分钟内收到的 404 / 405 响应数，超过阈值（默认 10 次）自动封禁 1 小时。

### 4.9 utils（基础设施层）

| 工具 | 说明 |
|------|------|
| `GuardedExecutor` | 5 种重试策略（stream / database / external_api / inference / persistence），支持固定延迟和指数退避 |
| `CircuitBreaker` | 连续 5 次失败熔断，60 s 后进入 half-open 尝试恢复 |
| 异常类（6 种） | `retryable` / `fatal` 双标记，决定 GuardedExecutor 的处理动作 |
| Prometheus 指标 | 推理延迟（Histogram）、推理失败、帧丢弃、GPU OOM、重试次数（Counter） |
| `context.py` | 基于 `threading.local` 的线程级 `client_id` / `task_id` 传递 |
| `worker_guard.py` | Worker 线程崩溃后自动重启（最多 3 次，冷却 2 s） |

---

## 五、配置体系

所有服务参数通过 YAML 配置文件加载。`inference_config.yaml` 是全局共享参数（帧率、队列长度、HLS 段长等）的**唯一来源**，其他服务的配置加载器会从中读取共享字段，避免多处重复定义产生不一致。

| 配置文件 | 核心参数 |
|---------|---------|
| `inference_config.yaml` | `stages`（Workflow 注册）、`batch_size`、`raw_fps`、`ca_maxlen`、`ca_segment_len` |
| `stream_config.yaml` | `default_fps`、`backpressure_ratio`（背压阈值）、`chunk_read_size` |
| `persistence_config.yaml` | `hls.workers`、`alarm.workers`、`cleanup_days` |
| `health_monitor_config.yaml` | `heartbeat_timeout`、`max_reconnect_attempts`、`task_max_duration` |
| `client_config.yaml` | 帧分辨率、`initial_stage` |

运行环境通过 `CLEANSIGHT_ENV` 环境变量切换（`dev` / `test` / `prod`），对应加载 `.env.dev` / `.env.test` / `.env`。

---

## 六、数据模型

### 数据库表（PostgreSQL）

| 表 | ORM 类 | 关键字段 |
|----|--------|---------|
| `clean_task` | `DBTask` | `task_id`（业务主键）、`source_ip`（映射为 `client_id`）、`current_step`（检测阶段）、`status` |
| `clean_alarm` | `DBAlarm` | `alarm_id`、`task_id`、`alarm_type`、`severity`、`message`、`detected_at` |

`source_ip` 字段在系统内部作为 `client_id` 使用，是 `ClientManager` 和 `ClientQueues` 中识别客户端的唯一标识。

### 关键内存模型

| 类 | 定义位置 | 说明 |
|----|---------|------|
| `FrameData` | `client/queues.py` | 单帧：`ndarray` 像素 + 时间戳 + 帧序号 |
| `InferenceResult` | `inference/models.py` | 单批推理结果：`Dict[task_name → DetectionOutput]` |
| `DetectionOutput` | `inference/data_models.py` | 单帧检测输出：边界框列表 + 任务专属字段 |
| `AlarmInfo` | `inference/data_models.py` | 告警描述：类型、级别、指标值、消息 |
| `AlarmRecord` | `inference/models.py` | 内存告警记录：含序列号，供增量查询 |
