> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 检测链路设计

本文写检测链路的总览与设计判据：流源 / 流算子怎么分、告警模式怎么选、在线还是离线。服务现状见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)，各检测点的标准与阈值见 [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)，扩展步骤见 [DESIGN_EXTENDING_DETECTION.md](DESIGN_EXTENDING_DETECTION.md)。

## 链路总览

```text
decoder → cq.ca_ready → StageAwareDispatcher → 推理子进程：各 Detector.infer_batch（GPU）
  → collector 按 req_id 组装 FrameDetection → 写回口（同一对象三处投递）
      ├─ 帧窗 _slide_window → ClientTemporalActor（2Hz）→ Operator.analyze → judge
      │                          ├─ overlay 文案 → 可视化叠字
      │                          └─ 实时告警 → alarm_sink（5s 闸）→ alarm_service 上报
      ├─ 最新快照 → VisualizationWorker 渲染 → _latest_rendered（/ai/video WS）+ ca_processed（HLS processed 轨）
      └─ 落盘缓冲 → recording → detections.jsonl → 离线 OfflineSegmenter → temporal.jsonl
run 拆除（stop_run）→ Operator.finalize → 结算告警 → alarm_sink
```

GPU 只做无状态的逐帧检测；跟踪、状态机、告警判定都在主进程 CPU 上。

## 逐帧前向归 Detector，跨帧状态归 Operator

每个 stage 拆 `detectors[]`（流源）与 `rules[]`（流算子）两层：

| 角色 | 粒度 | 实例 | 状态 |
|------|------|------|------|
| 流源 `Detector` | 一个模型一条流，`name` = 流名 = `FrameDetection.by_source` 的 key | 多 run 共享，跑在推理子进程 | 无 |
| 流算子 `Operator` | 一条规则一个 | 每 run 一个 | 全部在 `self._sm`，`analyze` 写、`judge` / `finalize` 读 |

- **判据**：只依赖单帧的计算放 Detector；需要历史（计数、跟踪、去抖、时序模型）的放 Operator。
- **analyze 与 judge 共享同一份 `_sm`**，不用 `TemporalEvent` 在对象间传测量结果，避免两份状态机同步。
- **身份两维正交**：`name` 是算子自身身份（日志、告警归属），`subscribes` 是输入流清单且必须显式声明。算子名 ≠ 流名，多个算子可订阅同一条流。
- **多流对齐在 collector 一次完成**：同帧各流按 req_id 装进同一个 `FrameDetection`，算子直接读 `by_source`，不需要自己 zip。前提是 Detector 把 `timestamps[i]` 原样写回。
- **反例**：
  - Detector 持 per-run 状态 → 共享实例在多个 run 间串台。
  - 往 `DetectorOutput` 加 `xxx_count` 之类领域字段 → 契约被单个检测点污染；单框派生量放 `DetBox.extra`，时序统计放 Operator。
  - Detector 自造时间戳 → 算子裁窗用 `FrameDetection.ts`、游标用 `DetectorOutput.timestamp`，两者不一致即错位。

## 帧窗是非破坏快照，跨帧累加必须走游标

每个 tick 拿到的帧窗与上一个 tick 大量重叠。计数、累加、喂 tracker 时须按 `_sm["last_ts"]` 只处理新帧，否则同一帧被重复计入、指标虚高；纯瞬时判断例外。基类 `_clip` 只裁输入窗口，算子自己派生的历史（如气泡的 `new_count_history`）要在 `_sm` 里按 `window_seconds` 自行裁剪。

## 告警模式由算子实现决定：实时靠上升沿锁存，结算靠 finalize

| 模式 | 产出方法 | 触发 | 防重 |
|------|----------|------|------|
| 实时 | `judge()` | Actor 每 tick 调用，条件成立的上升沿 | `_sm["alarming"]` 锁存（0→1 发，1→0 复位），再叠 CQ 的 5s 闸 |
| 结算 | `finalize()` | run 拆除时调用一次（`stop_run`） | 不需要 |

- YAML 的 `realtime` 只决定该规则订阅的流是否进 `signals_10s` 指标映射，不决定告警模式。
- `Alarm.metric` 由算子显式填，下游不从文案反推。
- overlay 文案只进画面；`Alarm` 走上报并进 CQ 告警环形日志供前端轮询，不进画面。

## 在线还是离线：看模型是否因果

- 在线 `TemporalOperator` 只能用因果模型（只看过去帧），每 tick 基于有限窗口出结果。需要未来帧或整段特征的模型（双向 RNN、MS-TCN、整段归一化特征）走离线 `OfflineSegmenter`。
- 离线只产分段事实与概率旁路，不判合规、不产告警；在线不写 `temporal.jsonl`。两条链只共用 `FrameDetection` 这一种输入。
- **入模帧率是模型契约**，与检测帧率解耦：`model_input_fps` 必须等于训练帧率，入模前按帧 ts 重采样。重采样只能降采样；配错不崩、静默误分类，所以必须显式配置并在构造期校验。

## 配置错误在边界失败，不降级

- stage 按 step_id 恒等路由，没有兜底 stage：未配置的 step 是参数错误，在 `/api/start` 或离线提交时直接 400，而不是落到别的实现上。
- 装配错误 fail-fast：Detector 与 rule 结构错误让后端起不来，Operator 构造错误让该次 `/api/start` 失败；运行时推理失败才逐帧降级为空结果。
