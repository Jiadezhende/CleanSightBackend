> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 检测标准

本文件描述当前代码实际执行的检测标准。业务标准如需调整，应优先修改配置或对应 Analyzer，并补充测试。

## 阶段路由

当前阶段路由写在 `InferenceManager._STEP_TO_STAGE`：

- `current_step == "1"`：`LEAK`
- `current_step == "2"`：`CLEAN`
- 其他值：`MOCK`

配置入口是 `config/inference_config.yaml`。CPU mock 配置另见 `config/inference_config_cpu.yaml`。

## LEAK 阶段

`LEAK` 当前包含两个检测点：

- `bubble`：气泡检测，实时告警。
- `bending`：内镜弯折动作检测，结算告警。

### 气泡检测

`BubbleDetector` 使用 YOLO 检测气泡实例，输出标准化 `DetectionOutput`。

`BirthRateAnalyzer` 每个 client 独立实例化，使用 ByteTrack 跟踪气泡实例并计算新气泡出生率：

```text
birth_rate = 滑动窗口内新气泡数总和 / 窗口帧数
```

当前默认阈值来自 `config/inference_config.yaml`：

- `birth_rate_threshold: 0.5`
- `window_seconds: 3.0`

当 `birth_rate > threshold` 且状态从未告警切到告警时，产生 high 级别实时告警，消息为“持续产生新气泡...疑似漏气”。持续触发期间不会重复产出，恢复后解除锁存。

### 弯折动作检测

`BendingDetector` 使用 YOLO 检测 `straight / bent` 状态。

`DebounceAnalyzer` 每个 client 独立实例化，通过连续帧去抖统计 `STRAIGHT -> BENT` 转换次数：

- `debounce_frames: 5`
- `required_bend_actions: 4`

实时阶段只产出 overlay 事件，不上报告警。任务 terminate、切换任务或服务停止时调用 `finalize()`；若累计 `bend_actions < required_bend_actions`，产生 warning 级别结算告警。

## CLEAN 与 MOCK 阶段

当前 `CLEAN` 和 `MOCK` 配置均使用 `MockDetector + MockAnalyzer`，参数设置为不触发告警：

- `brightness_threshold: 0.0`
- `consecutive_trigger: 999`

这表示当前代码中 CLEAN 阶段是占位透传，不代表最终业务标准。

## 告警去重

实时告警和结算告警都会经过 `ClientQueues.try_pass_alarm_gate()`。当前固定冷却窗口为 5 秒，同一 task、metric、mode 在窗口内只允许通过一次。

## 代码来源

- `config/inference_config.yaml`
- `app/services/inference/core/manager.py`
- `app/services/inference/workflows/bubble.py`
- `app/services/inference/workflows/bending.py`
- `app/services/inference/workflows/mock.py`
- `app/services/client/queues.py`
- `tests/test_alarm_increment.py`
- `tests/test_inference_stage_routing.py`

