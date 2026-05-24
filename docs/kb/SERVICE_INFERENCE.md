> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Inference Service

推理服务负责 stage 路由、模型推理、时序分析、可视化和结算告警。

## 核心组件

- `InferenceManager`：生命周期和 per-client 推理资源协调。
- `StageFactory`：从 YAML 创建 Detector 和 Analyzer specs。
- `StageAwareDispatcher`：从所有 client 取 `ca_ready`，按 stage 分组。
- `ModelWorkerService`：每个 stage 启动推理线程。
- `MultiModelWorkerPool`：同一 stage 内多模型 batch 推理，可选 CUDA Stream。
- `ClientTemporalActor`：每 client 一个时序分析 actor。
- `VisualizationWorkerPool`：定时拉取并渲染视频帧。

## 三池独立时钟

当前代码不是“推理 -> 时序 -> 可视化”的串行队列，而是三套独立节奏：

- 推理：按 stage batch 消费。
- 时序：每 client 约 1Hz 分析滑动窗口。
- 可视化：单线程约 15 FPS 轮询最新快照。

三者通过 ClientQueues 中的原子槽位和滑动窗口解耦。

## Detector 与 Analyzer

Detector：

- 无 per-client 状态。
- 多 client 共享实例。
- 负责推理和准备可视化数据。

TemporalAnalyzer：

- 每 client 独立实例。
- 持有 `self._sm` 状态机。
- 负责时序判断、实时告警和结算告警。

## 当前 stage

`config/inference_config.yaml`：

- `LEAK`：`bubble` + `bending`
- `CLEAN`：mock 透传
- `MOCK`：未知 step fallback

`InferenceManager._STEP_TO_STAGE`：

- `"1" -> "LEAK"`
- `"2" -> "CLEAN"`

## 告警输出

实时告警由 `ClientTemporalActor._persist_alarms()` 产生。结算告警由 `finalize()` 在 remove、task switch 或 stop 时收集。两者都经过 alarm gate，然后提交持久化服务。

## 代码来源

- `app/services/inference/core/manager.py`
- `app/services/inference/core/service.py`
- `app/services/inference/core/dispatcher.py`
- `app/services/inference/workers/base.py`
- `app/services/inference/workers/temporal.py`
- `app/services/inference/workers/visualization.py`
- `app/services/inference/workflows/`
- `config/inference_config.yaml`

