# 并发模型与锁设计

本文描述 CleanSightBackend 中各模块的线程模型、锁层级与防死锁策略。

---

## 一、线程全景

系统在运行时存在以下常驻线程（不含 FastAPI 的 uvicorn IO 线程）：

| 线程 | 数量 | 时钟 | 创建位置 |
|------|------|------|---------|
| StreamService selector（POSIX） | 1 | event-driven | `StreamService.__init__` |
| FFmpegDecoder stderr reader | 每客户端 1 | daemon | `FFmpegDecoder.start()` |
| FFmpegDecoder stdout reader（Windows） | 每客户端 1 | daemon | `FFmpegDecoder.start()` |
| StageAwareDispatcher | 1 | ~10 ms 轮询 | `StageAwareDispatcher.start()` |
| ModelWorkerPool inference loop | 每 Stage 1 | batch-driven | `ModelWorkerService.start()` |
| ClientTemporalActor | 每客户端 1 | 1 Hz | `InferenceManager.set_task()` |
| VisualizationWorker | 配置数量（默认 2） | ~15 fps | `VisualizationWorkerPool.start()` |
| HLS Worker | 配置数量（默认 2） | queue-driven | `PersistenceManager.start()` |
| Alarm Worker | 1 | queue-driven | `PersistenceManager.start()` |
| Cleanup Worker（可选） | 1 | 1 h 轮询 | `PersistenceManager.start()` |
| GlobalHealthMonitor | 1 | 1 s 轮询 | `GlobalHealthMonitor.start()` |

所有 Worker 线程均为 daemon 线程，以 `threading.Event` 作为停止信号，避免使用 `thread.daemon = True` 后强制终止导致数据丢失。

---

## 二、ClientQueues 锁层级

`ClientQueues` 是多生产者多消费者的共享内存中枢，通过 7 把 `threading.RLock` 保护各字段，锁之间存在明确的获取顺序。

### 锁清单

| 锁 | 类型 | 保护字段 |
|----|------|---------|
| `_task_lock` | RLock | `task`、`task_started_at` |
| `_raw_lock` | RLock | `ca_raw`、`latest_raw_frame`、`latest_raw_timestamp` |
| `_viz_lock` | RLock | `ca_processed`、`_latest_rendered` |
| `_inference_lock` | RLock | `_latest_inference` |
| `_frontend_lock` | RLock | `_stage`、`_latest_temporal` |
| `_slide_window_lock` | RLock | `_slide_window`（Dict） |
| `_alarm_lock` | RLock | `_alarm_log`、`_alarm_seq`、`_alarm_gate` |

### 防死锁顺序

当一个操作需要同时持有多把锁时，必须按以下顺序获取：

```
_task_lock
  → _raw_lock
    → _viz_lock
      → _inference_lock
        → _frontend_lock
          → _slide_window_lock
            → _alarm_lock
```

实际代码中，绝大多数操作只需一把锁，极少数需要两把。`full_clear()`（客户端清理）是需要最多锁的操作，它严格按照上述顺序依次获取后再释放。

### 持久化触发位置与锁的关系

`append_ca_raw()` / `append_ca_processed()` 在锁内完成帧追加和满段判断，然后**在锁外**调用 `PersistenceManager.persist_hls_segment()`，避免在持有 `_raw_lock` / `_viz_lock` 期间触发跨服务调用导致锁竞争。

---

## 三、InferenceManager per-client 锁

`InferenceManager` 为每个 client_id 维护一把 `threading.Lock`（`_client_locks`），保证 `set_task()` 和 `remove_client()` 对同一客户端的互斥执行。

**关键路径**：

```
set_task(client_id, new_task)：
  with _client_locks[client_id]:
    1. 停旧 TemporalActor → finalize_and_stop()
    2. 收集结算告警 → persist_settlement_alarms()
    3. 更新 ClientQueues 的 task 字段
    4. 创建新 TemporalActor → start()

remove_client(client_id)：
  with _client_locks[client_id]:
    1. 停 TemporalActor → finalize_and_stop()
    2. 收集结算告警 → persist_settlement_alarms()
    3. 刷新残余 HLS 帧
    4. 清理 ClientQueues 状态
```

两个操作的互斥保证了：在切换任务或清理客户端期间，不会有推理结果写入已销毁的 TemporalActor。

---

## 四、ClientManager 细粒度锁

`ClientManager` 采用两级锁，避免全局锁成为高并发瓶颈：

```python
# 快速路径（>95% 情况）：不需要任何锁
if client_id in self._clients:
    return self._clients[client_id]

# 慢速路径（首次创建）：使用 per-client 锁
with self._client_locks[client_id]:          # 细粒度锁
    if client_id in self._clients:           # 双重检查
        return self._clients[client_id]
    cq = ClientQueues(...)
    with self._clients_lock:                 # 全局锁（仅在此处）
        self._clients[client_id] = cq
    return cq
```

读操作（`get_client`、`get_all_clients`）无需全局锁；写操作（创建、删除）在全局锁保护下完成字典修改。

---

## 五、StreamService 线程模型（POSIX vs Windows）

StreamService 使用平台特定的 stdout 读取策略，原因是 POSIX 的 `selectors` 不支持 Windows 的命名管道。

### POSIX（Linux / macOS）

```
StreamService.__init__：
  创建 selectors.DefaultSelector()
  启动 selector 后台线程（_selector_loop）

FFmpegDecoder.start()：
  启动 FFmpeg 子进程
  向 StreamService 注册 proc.stdout（selector.register）

selector 后台线程：
  select() 等待 stdout 就绪 → 调用 decoder.on_stdout_ready()
  → 读取数据 → 追加 buffer → _process_frames()
```

### Windows

```
FFmpegDecoder.start()：
  启动 FFmpeg 子进程
  单独启动 _windows_reader_loop daemon 线程

_windows_reader_loop：
  while not stop:
    data = proc.stdout.read(chunk_size)   # 阻塞读
    → 追加 buffer → _process_frames()
```

两种模式下，`_process_frames()` 逻辑完全相同，仅数据到达方式不同。

---

## 六、API 层 per-client asyncio.Lock

`/api/start` 和 `/api/terminate` 使用 per-client `asyncio.Lock`，防止同一客户端的并发请求产生竞态（例如两个 start 请求同时执行，导致两个流同时启动）。

```python
async def start(client_id, ...):
    lock = await _get_client_lock(client_id)
    async with lock:
        # 幂等检查 + 重建逻辑
        ...
```

该锁是 asyncio 协程级别的，不会阻塞事件循环中的其他 IO 操作。

---

## 七、YOLO 模型惰性加载（双重检查锁）

`YOLODetector` 的模型在首次推理时加载，使用 `threading.Lock` + 双重检查保证线程安全：

```python
def _ensure_model_loaded(self):
    if self._model is not None:          # 快速路径（无锁）
        return
    with self._model_load_lock:          # 慢速路径（加锁）
        if self._model is None:          # 再次检查
            self._model = YOLO(path)
```

这样多个推理线程在模型加载完成后走无锁快速路径，不产生锁竞争。
