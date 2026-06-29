# Processed HLS 回放快放 ~2x 根因观测与改造提案

> **变更状态**：已实现（2026-06-29）　<!-- Fix A + Fix B(15fps) + B2 渲染削峰 全部落地 -->
> **知识库**：待沉淀
>
> 相关：[20260628_INFER_PRESSURE_OBSERVABILITY.md](20260628_INFER_PRESSURE_OBSERVABILITY.md)（本次定位所依赖的 `[INFER_PRESSURE]` / `[VIZ_THROUGHPUT]` 观测埋点）、[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（分层数据流核实）。

## 概述

- **现象**：processed 链路的 HLS 回放明显比 raw 快约 **2x**。远程机（cleansight-4090）落盘元数据对比坐实：raw/processed 编码时长比 2.19~2.47，段数比恒为 **3:1**，两条链路覆盖**同一墙钟窗口**。
- **根因**：**时钟/速率失配**，非积压、非丢帧。processed 实际成帧率 ≈ **11fps**，却被**写死按 20fps 编码** → 播放 = 20/11 ≈ 1.8x 快放。`[BACKPRESSURE]`/backlog 类指标天然测不到（帧没进队列、没被丢，只是产得慢）。
- **改造方向**：
  1. **Fix A（治本，建议先做）**：processed 段按帧时间戳**实测 fps** 编码，播放对齐墙钟，对任何真实帧率都成立。
  2. **Fix B（可选，提升真实流畅度）**：消除速率亏空的两道结构性瓶颈——throttle 最小间隔门、渲染尾延迟尖峰。
- **影响面**：Fix A 改 `persistence/strategies/hls_strategy.py` 的 processed 编码；Fix B 改 `client/queues.py` 的 throttle 与 `inference/workers/visualization.py` 的渲染。**本提案不含已实现项**，仅落方案。

---

## 观测报告

### 落盘证据（远程 cleansight-4090，纯产物对比）

同一墙钟窗口下 raw 与 processed 元数据对比（`/home/ubuntu/CleanSightBackend-test/database`）：

| task | raw 段数 | processed 段数 | 编码时长比 raw/processed |
|------|---------|---------------|----------------------|
| 113 | 21 | 7 | 2.29 |
| 112 | 9 | 3 | 2.47 |
| 111 | 3 | 1 | 2.19 |

- 段数比**精确 3:1**。raw 每段 300 帧 @30fps，processed 每段 300 帧；同一时间窗 raw 抓 ~3× 的帧。
- raw 编码时长 ≈ 真实墙钟（地板真值），processed 编码时长被压到约一半 → 时间压缩 ~2.2-2.5x。

### 运行时链路量化（dev 实测，`[VIZ_THROUGHPUT]` + decoder 帧计数）

每 ~20s 窗（decoder 600 raw 帧）逐级速率：

| 环节 | 速率 | 损耗 |
|------|------|------|
| decoder 解码 | 600 帧 = **30fps** | 基准 |
| 过 throttle → `ca_ready` | `ready=273` ≈ **13.6fps** | **throttle 砍 ~55%** |
| 渲染 → `ca_processed` | `out=11fps` | 渲染尖峰再砍 ~20% |
| 按 20fps 编码 | 播放 20/11 ≈ **1.8x 快放** | 对上"快一倍" |

关键日志：

```
[VIZ_THROUGHPUT] target=20fps render=21.0ms(max 79.3ms, budget 50ms) || test.s113 out=11.0fps stale=44% (render-bound)
[VIZ_THROUGHPUT] target=20fps render=16.0ms(max 28.4ms, budget 50ms) || test.s113 out=12.7fps stale=35% (supply-bound)
received 600 frames (raw=600, ready=273, dropped=0)
```

- `dropped=0` 且全程**无 `[INFER_PRESSURE]` 行** → 无积压、无丢帧。坐实"速率亏空"而非"积压"。
- tag 随窗来回切换 supply/render-bound，正是两道瓶颈交替主导的体现。

### 根因拆解

**① throttle 结构性砍到 ~13.6fps（主因）**

throttle 是 **50ms 最小间隔门**（`interval = 1/inference_fps = 1/20 = 50ms`）。30fps 输入帧间隔 33ms：

```
放 → 下一帧 33ms<50ms 拒 → 再下一帧 66ms≥50ms 放 → …
```

实际放行约每 66ms 一帧 = **15fps 上限**，实测 13.6（门+抖动更低）。**用最小间隔门从 30fps 源永远拿不到 20fps**——这是算法决定的天花板，与负载无关。

**② 渲染尾延迟尖峰把 13.6 压到 11（次因）**

平均渲染 16-24ms **未超** 50ms 预算，问题在**尖峰**（max 71-115ms）。`_latest_inference` 是单槽 latest-wins：一次 100ms 渲染期间上游更新数次、只渲染最后一帧 → 把 13.6 塌成 11。**渲染要优化的是尾延迟尖峰，不是均值。**

> 观测判据微调备注：`[VIZ_THROUGHPUT]` 现以 `max≥budget` 即标 render-bound，偏激进。真实形态是"throttle 定地板 ~13.6，渲染尖峰再扣 ~2fps"，两者皆真、交替出现。

---

## 改造提案

### Fix A —— processed 段按实测 fps 编码（治本，建议先落）

**文件**：`app/services/persistence/strategies/hls_strategy.py` → `_persist_processed_segment`

**现状**：`cv2.VideoWriter(..., self.processed_fps=20, ...)` 且 `segment_duration = len(frames) / self.processed_fps`，固定 20fps。当实际成帧 ≠ 20 即快放/慢放。

**方案**：从该段帧的时间戳跨度算有效 fps，`VideoWriter` 与 `EXTINF` 用**同一个** `eff_fps`：

```python
span = frames[-1].timestamp - frames[0].timestamp
eff_fps = (len(frames) - 1) / span if span > 0 else self.processed_fps  # 兜底回标称
```

- 三套时间线（EXTINF / tfdt / fragment 媒体时长）仍自洽——EXTINF 仍等于 fragment 媒体时长，不违反 [hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py) `_ts_offset_seconds` 处的强警告。
- 逐段各取自身有效 fps，自动吸收速率抖动，**无论真实率 11/13/20 播放都对齐墙钟**。
- 可同样加固 raw 段（raw 实测稳定 30fps，优先级低）。

> 取舍：Fix A 让播放正确但不提升流畅度（仍是 ~11fps 的画面）。若该巡检场景对实时观看流畅度不敏感，Fix A 即可收口。

### Fix B —— 消除速率亏空（可选，要更流畅再做）

#### B1. throttle 改分数重采样（解决 30→20 不可达）

**文件**：`app/services/client/queues.py` → `append_ca_ready_with_throttle`

最小间隔门换成**累加器式分数重采样**（保留进度余量，按 `target/source` 比例放行），30fps→20fps 可真正达成（约每 3 帧取 2）；或**退而求其次**把 `settings.inference_fps` 设为 **15**（30 的整除，最小间隔门即可干净放行每隔一帧），并令编码/标称随之对齐。

> 注：`inference_fps` 是 throttle、VizWorker target、processed 编码**共用单一真源**（见 settings.py 注释），调它会同步影响三处——这正是为何 Fix A 的"按实测编码"是更稳的解耦。

#### B2. 渲染尾延迟削峰

**文件**：`app/services/inference/workers/visualization.py` → `FixedVisualizer`

- 圆角框 `_draw_rounded_rect` 当前为**每个小框拷贝整帧 + 整帧 alpha 混合**；改为只在框包围盒 ROI 上拷贝/混合（画面 640×480，单帧多框时整帧操作是尖峰来源）。mask 混合同理收 ROI。
- 已落项（本次先行、低风险）：去掉 `_render` → `render()` 间一帧两次整帧拷贝中的冗余一次。

---

## 落地记录（2026-06-29）

三项一并落地（决策：Fix A 治本消抖动 + B1 取整除 15fps 抬地板 + B2 渲染削峰，三者正交互补）：

- **Fix A**：[hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py) 新增静态 `_effective_fps(frames, fallback)` = `(N-1)/(ts_last-ts_first)`，`span≤0` / 单帧 / 反推值落 `[1,60]` 带外回退标称；`_persist_processed_segment` 的 `VideoWriter` 与 `segment_duration`(EXTINF) 同源用 `eff_fps`。raw 段保留现状。
- **B1**：[settings.py](../../app/settings.py) `inference_fps 20→15`（单一真源，联动 throttle / VizWorker / processed 标称）；[queues.py](../../app/services/client/queues.py) `append_ca_ready_with_throttle` 门限改 `< interval*0.9`，吸收 wall-clock 抖动锁死真实 15fps（33ms 中间帧仍被拒，不会放成 30fps）。
- **B2**：[visualization.py](../../app/services/inference/workers/visualization.py) `_draw_rounded_rect` 改 ROI 局部 copy/混合（与整帧实现**像素级等价**，矩形外旧实现本就还原原像素，纯加速）；`_draw_masks` 改 mask 包围盒 ROI、只染 mask 像素（顺带修掉旧实现整帧压暗的副作用；当前 workflow 全 BBOX、无 mask，无可见回归）。

## 验证

| 项 | 结果 |
|----|------|
| Fix A 单测 | `tests/test_hls_eff_fps.py`：eff_fps 正常反推 / span≤0 / 单帧 / 带外 / 乱序 回退 —— 通过 |
| B2 单测 | `tests/test_rounded_rect_roi.py`：ROI 与整帧基准像素级一致 + 矩形外不变 + 越界裁剪 —— 通过 |
| 回归 | `pytest tests/` 229 passed |
| dev 端到端（**待人工**） | 跑测试流确认 `[VIZ_THROUGHPUT] target=15fps`、`render max` 落 ~66ms 预算内、`out≈15fps`；回放 processed 与 raw 时长/速度一致、段间不抖 |

> 验证仅在 dev 跑，不触发写库/发告警的端到端链路。
