# ca_ready 抽帧：墙钟门 → 相位累加器

> **变更状态**：实施工单（2026-06-30）
> **知识库**：待落地后沉淀
>
> 配套前置：[20260630_SYNTHETIC_CFR_TIMEBASE_TASK.md](20260630_SYNTHETIC_CFR_TIMEBASE_TASK.md)（时间基准改造，解决卡顿下时序感知；本工单先行，两步独立可绿）。
> 相关：[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（数据流核实）。

## 工单定位

把 [`ClientQueues.append_ca_ready_with_throttle`](../../app/services/client/queues.py) 的 **wall-clock 限流门** 换成 **Bresenham 相位累加器均匀抽帧**，删除墙钟时间戳字段，背压语义原样保留。承载"raw 帧率统一 + 可设降采样率"两条诉求。

## 现状根因

`append_ca_ready_with_throttle` 现以 `time.time()` 计相邻保留帧间隔，`≥ 0.9×(1/inference_fps)` 才放行。喂入的已是 ffmpeg `fps` 滤镜规范化后的 CFR-30 流，再用**另一个时钟**（decoder 从 pipe 读出的墙钟到达时刻，含线程调度抖动）去对齐，使大量候选帧落在 60ms 门槛边缘被拒——实测把目标 15fps **漏成 ~12fps**（详见现场日志 `received raw=900 ready=357`，比值 0.40 而非 0.50）。

settings 注释里"30→20 是算法天花板"也是这个墙钟门的副作用，非本质限制。

## 关键前提

- 输入为 ffmpeg `-vf fps=raw_fps` 规范化后的 **CFR `raw_fps` 帧序列**（dup/drop 相对源 PTS），帧计数稳定；
- `ca_ready` 为无锁 SPSC deque，**decoder 是唯一写入方**；抽帧状态仅本方法（decoder 线程）读写，**无并发、不加锁**；
- 降采样率 = `inference_fps / raw_fps`，二者均来自 `app/settings.py` 单一真源。

## 设计：Bresenham 相位累加器

每个输入帧累加 `inference_fps`，跨过 `raw_fps` 阈值时放行一帧并扣减阈值：

```
self._decimate_phase += inference_fps      # 每个输入帧累加
if self._decimate_phase < raw_fps:
    return False                            # 本帧不选
self._decimate_phase -= raw_fps            # 选中
# → 再过背压门
```

- 长期保留率**精确** = `inference_fps/raw_fps`，零墙钟抖动；
- 支持**非整除比**：30→20 得 keep-keep-drop（精确 2/3），解除旧"天花板"；
- 相位**每输入帧推进一次**（不是每选中一次），这是累加器正确性的不变式。

## 范围

- [`queues.py`](../../app/services/client/queues.py)：
  - `__init__` 增 `raw_fps: int = 30` 参数；删 `self.last_inference_timestamp`，加 `self._decimate_phase: int = 0`；
  - `append_ca_ready_with_throttle`：相位累加器选帧 → 背压门（`len(ca_ready) >= maxlen` 丢）→ append。**背压判定保留、置于选帧之后**（仅对选中帧生效，过载语义不变）。
- plumbing：`raw_fps` 经 `settings → stream/service.py → ClientManager.get_client → ClientQueues` 传入，与 `inference_fps` 并行；[`client/config.py`](../../app/services/client/config.py) 增 `raw_fps` 属性（镜像 `inference_fps`，读 settings 单一真源）。

## 不在本工单

- Frame.timestamp 仍为墙钟到达时刻（卡顿 burst 塌缩问题留配套工单）；
- decoder 侧合成时钟、ByteTracker `frame_rate=10` 对齐 —— 另案。

## 验收

- 稳态：`ca_ready` 实测率 = `inference_fps`（±0），不再 ~12fps；
- 背压：`ca_ready` 满时选中帧被丢、`frames_dropped` 递增，语义同旧；
- 单测：给定 raw=30/inference=15 输入 N 帧，断言保留 = N/2；raw=30/inference=20 断言保留 = 2N/3 且分布均匀（keep-keep-drop）。
