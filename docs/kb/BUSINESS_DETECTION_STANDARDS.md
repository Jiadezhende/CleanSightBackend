> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 检测标准

本文件描述当前代码实际执行的检测标准。业务标准如需调整，应优先修改配置或对应 Operator，并补充测试。

## 阶段路由

`InferenceService.resolve_stage(step_id)` 恒等路由：`str(step_id)` 命中 `config/inference_config.yaml` 里有 detector 的 stage 键则用之。当前 stage 键只有 `"1"`(alias LEAK) / `"2"`(alias CLEAN)，**无兜底 stage**。stage 是 CQ 不可变身份的一部分（构造时定死），无独立 `_STEP_TO_STAGE` 表。

未配置的 step 一律按参数错误处理：

- `/api/start`：`current_step` 非数字、YAML 未定义、或定义了但无 detector → 400（锁外、动旧 run 之前判，旧 run 不受影响）。
- 离线提交（admin 离线作业 / CLI）：step 未定义或 `offline` 为空 → 400 / CLI 退出码 1。
- 运行时推理失败不切 stage：逐帧降级为空结果（画面照常、只是没框），降级帧不进检测结果落盘。

## LEAK 阶段

`LEAK` 当前包含两个检测点：

- `bubble`：气泡检测，实时告警。
- `bending`：内镜弯折动作检测，结算告警。

### 气泡检测

`BubbleDetector` 使用 YOLO 检测气泡实例，输出标准化 `DetectorOutput`。

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

`CLEAN`（stage `"2"`）有两个 detector 与一条在线规则，另挂离线动作分割；**当前不产告警、不判合规**，尚不代表最终业务标准。

- `clean_large`（大目标组：手 / scope_control_body / scope_mid_section），`CleanLargeDetector`。
- `clean_small`（小目标组：syringe / air_gun / scope_distal_end），`CleanSmallDetector`。

### 在线动作识别（`clean_monitor`）

`CleanOperator`（rule `clean_monitor`，`subscribes: [clean_large, clean_small]`，`realtime: true`）每 run 独立实例化，持时序模型 `gru-final.pt`：

- `window_seconds: 10.0`：取最近 10s 帧窗。
- `model_input_fps: 7.5`：入模前按帧 ts 把窗口降采样到训练帧率，须等于训练 fps，配错不报错、静默误分类。
- 动作类别（`actions`）：idle / air_injection / flush / long_brush_insert / long_brush_withdraw。

`judge` 只出 overlay 文案 `Action: <label>`（当前动作），不产告警。

### 离线动作分割（默认启用）

`stage."2".offline` 默认启用 `CleanNodepGRUSegmenter`（权重 `clean-offline-gru-nodep.pt`，`model_input_fps: 7.5`、`confidence_override: 1.0`、`min_duration_s: 0.2`）。run 结束后经 admin「运行离线推理」手动提交，对整段检测序列逐帧分类，产出 6 类动作分段（idle / water_injection / flush / long_brush_insert / long_brush_withdraw / short_brush_cleaning；idle 为背景，不成段）写 `temporal.jsonl`，并落逐帧类别概率 `label_probs.npz` 供页面画曲线。短于 `min_duration_s` 的段丢弃。离线结果**不判合规、不产告警、不入库**；自动触发未实现。权重缺失时 CLEAN 离线作业 failed，不影响在线。

可调整位置：`config/inference_config.yaml` 的 `stages."2".offline`（换 `class` 须同时换 `params`，备选类 ↔ 权重映射见 YAML 注释）。

## 试纸比色（非视频流）

过氧乙酸试纸色卡比色是独立的图片判定服务（`POST /algorithm/colorstrip`），**不走 stage 路由，与 LEAK / CLEAN 无关**：

- 判据：以瓶身色卡 2000 刻度块为参考下限，试纸比它更深（L* 更低）即合格；相对比色，同一张图里消去光照。色相窗口只用来认「谁是 800、谁是 2000」，不参与合格判定。
- 规范：一张图待测试纸条数须等于 `spec.expected_strips`（当前 1），条数不对即拒判（`ok=False`，不等于不合格）。
- 可调整位置：`app/services/algorithm/colorstrip/params.yaml`（单一真源；换光源优先新开 profile，改 `default` 档须用仓库外验收工装重跑并 `--freeze` 重冻结基线）。

算法与接线细节见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)。

## 告警去重

实时告警和结算告警都经 `ClientQueues.append_alarm_record_with_gate()`。固定冷却窗口 5 秒，同一 `(task_id, metric, mode)` 窗口内只放行一次；过闸编排在 `inference/online/temporal/alarm_sink.persist_alarms`。

## 代码来源

- `config/inference_config.yaml`
- `app/services/inference/online/service.py`（`resolve_stage`）、`app/services/inference/config.py`（`require_offline`）
- `app/services/inference/online/detection/impl/{bubble,bending,clean}.py`（Detector 子类）
- `app/services/inference/online/temporal/impl/{bubble,bending,clean}.py`（Operator 子类）
- `app/services/inference/offline/impl/clean.py`（离线 Segmenter 子类）
- `app/services/inference/online/temporal/alarm_sink.py`
- `app/services/client/queues.py`
- `app/services/algorithm/colorstrip/{grader.py,params.yaml}`
- `tests/test_alarm_increment.py`
- `tests/test_inference_stage_routing.py`
