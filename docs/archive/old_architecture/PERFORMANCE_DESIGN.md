# 性能设计

本文描述 CleanSightBackend 在吞吐量、延迟和资源利用率上的关键设计决策。

---

## 一、背压自适应丢帧

**问题**：GPU 推理速度（~15 fps）低于摄像头帧率（30 fps），长期运行会导致 `ca_ready` 队列无限积压，推理延迟持续增大。

**策略**：在 `FFmpegDecoder._process_frames()` 中检测 `ca_ready` 队列深度，超过容量 90% 时仅丢弃推理帧，`ca_raw` 帧不受影响。

```python
pending_count = ca_ready.qsize()
capacity = ca_ready.maxsize

if pending_count / capacity >= backpressure_ratio:   # 默认 0.90
    frames_dropped += 1
    # 不写 ca_ready（丢推理帧）
else:
    ca_ready.append(frame)

# 无论是否丢帧，始终写 ca_raw（保证录制完整）
ca_raw.append(frame)
```

**效果**：推理队列深度始终保持在可控范围，推理延迟稳定；录制视频完整无缺。

---

## 二、三池独立时钟

推理引擎由三个完全独立的 Worker 池组成，各自以不同频率运行，互不阻塞：

| 池 | 时钟 | 瓶颈 |
|----|------|------|
| ModelWorkerPool | batch-driven（~15 fps） | GPU 推理耗时 |
| TemporalActor | 1 Hz | 状态机计算（通常 < 1 ms） |
| VisualizationPool | ~15 fps | CPU 图像叠加 |

三池之间通过 `ClientQueues` 的原子快照槽位（`_latest_inference`、`_latest_temporal`）通信，不使用队列串联。这样：
- `TemporalActor` 的 1 Hz 慢节拍不阻塞推理和可视化。
- 可视化渲染失败不影响推理和时序分析。
- 任意一池的 Worker 崩溃重启不传播到其他池。

---

## 三、CUDA Stream 并行多模型推理

当一个 Stage 配置了多个模型（例如同时运行气泡检测和弯曲检测），`MultiModelWorkerPool` 使用独立的 CUDA Stream 并行执行各模型推理，最后统一同步：

```python
async_results = []
for model, cuda_stream in zip(models, cuda_streams):
    with torch.cuda.stream(cuda_stream):
        batch_res = model.infer_batch(frames, contexts)
        async_results.append((model.name, batch_res))

torch.cuda.synchronize()    # 等待所有 stream 完成

# 按帧索引合并各模型结果
merged = [{} for _ in range(len(frames))]
for model_name, batch_res in async_results:
    for i, res in enumerate(batch_res):
        merged[i][model_name] = res
```

多模型总耗时接近单模型中耗时最长的那个，而非各模型耗时之和。

---

## 四、Dispatcher 自适应超时

`StageAwareDispatcher.get_batch_for_stage()` 根据当前队列深度动态调整等待超时，在低负载时减少无效轮询，高负载时快速响应：

| 队列深度 | 等待超时 |
|---------|---------|
| >= batch_size × 2 | 1 ms |
| >= batch_size | 2 ms |
| < batch_size | 3 ms |

如果等待超时后队列仍为空，推理循环 `sleep(10 ms)` 后继续，避免 CPU 忙等。

---

## 五、原子快照设计

可视化渲染需要同时读取推理结果（来自 ModelWorkerPool）和时序事件（来自 TemporalActor）。如果直接从队列读取会产生"读到不同帧的推理结果和时序状态"的不一致问题。

解决方案：使用单槽位原子快照：

```
ModelWorkerPool 完成推理后：
  set_latest_inference(result)     # 原子写，带锁

TemporalActor 完成 Tick 后：
  set_latest_temporal(events)      # 原子写，带锁

VisualizationPool 渲染时：
  inf = get_latest_inference()     # 原子读
  tmp = get_latest_temporal()      # 原子读
  # inf 和 tmp 可能来自不同帧，但两者都是最新状态
  # 对于标注叠加来说，这是可接受的近似
```

这个设计以"可能有 1 帧的时序/推理不严格对齐"换取了"无锁序列化"，对最终标注效果影响可忽略。

---

## 六、HLS 段积累触发与锁外持久化

`ClientQueues.append_ca_raw()` / `append_ca_processed()` 在帧数达到 `ca_segment_len` 时触发 HLS 落盘，但持久化调用发生在锁外：

```python
def append_ca_raw(self, frame: FrameData):
    task_snapshot = self.get_task()          # 先快照，减少锁持有
    frames_to_persist = None

    with self._raw_lock:
        self.ca_raw.append(frame)
        if task_snapshot and len(self.ca_raw) >= self.ca_segment_len:
            frames_to_persist = list(self.ca_raw)[:self.ca_segment_len]
            for _ in range(self.ca_segment_len):
                self.ca_raw.popleft()
    # 锁已释放，在此调用持久化（耗时操作不占锁）
    if frames_to_persist:
        persistence_manager.persist_hls_segment(
            client_id=self.client_id,
            task_id=task_snapshot.task_id,
            segment_type="raw",
            frames=frames_to_persist,
        )
```

持久化入队操作通常 < 0.1 ms（写入有界 Queue），不会阻塞 FFmpegDecoder 的帧写入。

---

## 七、惰性模型加载

YOLO 模型权重文件较大（几十到几百 MB），在服务启动时一次性加载所有模型会显著延迟就绪时间，且如果某个 Stage 当天不使用，则浪费显存。

`YOLODetector` 采用惰性加载：首次 `infer()` 调用时才加载模型到 GPU。加载完成后缓存在实例属性中，后续调用走无锁快速路径（见 [CONCURRENCY_DESIGN.md](CONCURRENCY_DESIGN.md) 第七节）。

---

## 八、推理帧率降频

原始摄像头通常输出 30 fps，但 GPU 推理能力约为 15–20 fps（取决于模型规模和 batch size）。为避免无效排队，`ca_ready.append_throttle()` 对推理队列写入做令牌桶降频：

```python
# inference_config.yaml
global:
  raw_fps: 30           # 录制帧率（ca_raw 全量写入）
  inference_fps: 20     # 推理目标帧率
  inference_decimation: 2   # 每 N 帧取 1 帧（与 inference_fps 配合）
```

降频后 `ca_ready` 的到达速率与 GPU 推理速度基本匹配，背压丢帧触发频率降至接近零。
