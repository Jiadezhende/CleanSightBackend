> 更新时间：2026-08-02
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 检测标准

本文件描述当前代码实际执行的检测标准。业务标准如需调整，应优先修改配置或对应 Operator，并补充测试。

## 阶段路由

`InferenceManager.resolve_stage(step_id)` 恒等路由：`str(step_id)` 命中 `config/inference_config.yaml` 的 stage 键则用之，否则回落 `MOCK`。当前 stage 键 `"1"`(alias LEAK) / `"2"`(alias CLEAN) / `MOCK`。stage 是 CQ 不可变身份的一部分（构造时定死），无独立 `_STEP_TO_STAGE` 表。

## LEAK 阶段

`LEAK` 当前包含两个检测点：

- `bubble`：气泡检测，实时告警。
- `bending`：内镜弯折动作检测，结算告警。

### 气泡检测

`BubbleDetector` 使用 YOLO 检测气泡实例，输出标准化 `FrameDetections`。

`BubbleOperator`（rule `bubble_leak`，`subscribes: [bubble]`，`realtime: true`）每 run 独立实例化，使用 ByteTrack 跟踪气泡实例并计算新气泡出生率：

```text
birth_rate = 滑动窗口内新气泡数总和 / 窗口帧数
```

当前默认阈值来自 `config/inference_config.yaml`：

- `birth_rate_threshold: 0.5`
- `window_seconds: 3.0`

当 `birth_rate > threshold` 且状态从未告警切到告警时，产生 high 级别实时告警，消息为“持续产生新气泡...疑似漏气”。持续触发期间不会重复产出，恢复后解除锁存。

### 弯折动作检测

`BendingDetector` 使用 YOLO 检测 `straight / bent` 状态。

`BendingOperator`（rule `bending_check`，`subscribes: [bending]`，`realtime: false`）每 run 独立实例化，通过连续帧去抖统计 `STRAIGHT -> BENT` 转换次数：

- `debounce_frames: 5`
- `required_bend_actions: 4`

实时阶段只产出 overlay 事件，不上报告警。任务 terminate、切换任务或服务停止时调用 `finalize()`；若累计 `bend_actions < required_bend_actions`，产生 warning 级别结算告警。

## CLEAN 阶段

`CLEAN`（stage `"2"`）当前 `rules: []`——**不建 Operator/Actor，不产告警**，仅由两个 detector 提供检测框可视化：

- `clean_large`（大目标组：手 / scope_control_body / scope_mid_section），`CleanLargeDetector`。
- `clean_small`（小目标组：syringe / air_gun / scope_distal_end），`CleanSmallDetector`。

离线动作分割模型（stage 粒度）将来挂 `stage."2".offline`（占位，未实现）。CLEAN 尚不代表最终业务标准。

## MOCK 阶段

未知 `current_step` 的 fallback + taskless 默认，纯透传：`MockDetector`（`brightness_threshold: 0.0` 永不触发）+ `MockOperator`（rule `mock_passthrough`，`consecutive_trigger: 999` 恒不告警）。

## 告警去重

实时告警和结算告警都经 `ClientQueues.append_alarm_record_with_gate()`。固定冷却窗口 5 秒，同一 `(task_id, metric, mode)` 窗口内只放行一次；过闸编排在 `inference/temporal/alarm_sink.persist_alarms`。

## 代码来源

- `config/inference_config.yaml`
- `app/services/inference/manager.py`
- `app/services/inference/detection/impl/{bubble,bending,clean,mock}.py`（Detector 子类）
- `app/services/inference/temporal/impl/{bubble,bending,clean,mock}.py`（Operator 子类）
- `app/services/inference/offline/impl/{clean,mock}.py`（离线 Segmenter 子类）
- `app/services/inference/temporal/alarm_sink.py`
- `app/services/client/queues.py`
- `tests/test_alarm_increment.py`
- `tests/test_inference_stage_routing.py`

